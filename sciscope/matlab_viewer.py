"""Open a bounded recording selection in an independent MATLAB desktop."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

from .config import dtype_for
from .storage import SegmentReader, export_segment

MAX_IMPORT_BYTES = 128 * 1024**2
STARTUP_TIMEOUT = 300

# A script with a local function keeps temporary variables out of the user's
# workspace. Data and names come from MAT, never from executable expressions.
MATLAB_SCRIPT = r"""sci_scope_open_recording(fileparts(mfilename('fullpath')));

function sci_scope_open_recording(folder)
    dataFile = fullfile(folder, 'data.mat');
    try
        values = load(dataFile);
        metadata = jsondecode(values.profile_json);
        [sessionFolder, segment] = fileparts(metadata.source);
        [~, session] = fileparts(sessionFolder);
        runName = sprintf('%s / %s [%g, %g) s', session, segment, values.time_s(1), values.time_s(end) + 1 / double(values.sample_rate));
        values.data(~logical(values.valid), :) = NaN;
        names = values.channel_names;
        units = values.units;
        count = size(values.data, 2);
        if ~isempty(which('Simulink.sdi.createRun'))
            signals = cell(1, count);
            for index = 1:count
                signal = timeseries(values.data(:, index), values.time_s, 'Name', names{index});
                signal.DataInfo.Units = units{index};
                signal.TimeInfo.Units = 'seconds';
                signal.TreatNaNasMissing = false;
                signals{index} = signal;
            end
            runID = Simulink.sdi.createRun(runName, 'vars', signals{:});
            imported = Simulink.sdi.getRun(runID);
            if imported.SignalCount ~= count
                error('SCIScope:ImportCount', 'Expected %d signals; imported %d.', count, imported.SignalCount);
            end
            for index = 1:count
                signal = imported.getSignalByIndex(index);
                signal.Checked = true;
                fprintf('SCI Scope: %s [%s], %d samples\n', signal.Name, signal.StoredUnits, signal.NumPoints);
            end
            Simulink.sdi.view;
            result = struct('ok', true, 'viewer', 'sdi', 'run_id', runID);
        else
            figure('Name', runName, 'NumberTitle', 'off');
            plot(values.time_s, values.data);
            grid on;
            xlabel('Time (s)');
            labels = names;
            for index = 1:count
                if ~isempty(units{index})
                    labels{index} = [names{index} ' [' units{index} ']'];
                end
            end
            legend(labels, 'Interpreter', 'none');
            result = struct('ok', true, 'viewer', 'figure');
        end
        result.signal_count = count;
        result.samples = size(values.data, 1);
        result.names = names;
        result.name = runName;
    catch exception
        result = struct('ok', false, 'identifier', exception.identifier, 'message', exception.message);
        fprintf(2, '%s\n', getReport(exception, 'extended', 'hyperlinks', 'off'));
    end
    % The viewer owns its imported data; the transfer copy is no longer needed.
    if exist(dataFile, 'file')
        delete(dataFile);
    end
    statusFile = fullfile(folder, 'status.json');
    handle = fopen(statusFile, 'w', 'n', 'UTF-8');
    if handle >= 0
        closer = onCleanup(@() fclose(handle));
        fprintf(handle, '%s', jsonencode(result));
    end
end
"""


def _registry_executables():
    if os.name != 'nt':
        return
    import winreg
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive, r'SOFTWARE\MathWorks\MATLAB', 0, winreg.KEY_READ | view) as key:
                    versions = [winreg.EnumKey(key, index) for index in range(winreg.QueryInfoKey(key)[0])]
                    for version in sorted(versions, reverse=True):
                        try:
                            with winreg.OpenKey(key, version) as release:
                                root = winreg.QueryValueEx(release, 'MATLABROOT')[0]
                                yield Path(root) / 'bin' / 'matlab.exe'
                        except OSError:
                            continue
            except OSError:
                continue


def find_matlab() -> Path | None:
    executable = shutil.which('matlab.exe' if os.name == 'nt' else 'matlab')
    if executable and Path(executable).is_file():
        return Path(executable).resolve()
    for candidate in _registry_executables():
        if candidate.is_file():
            return candidate.resolve()
    if os.name == 'nt':
        for variable in ('ProgramW6432', 'ProgramFiles', 'ProgramFiles(x86)'):
            root = os.environ.get(variable)
            if root:
                for release in sorted((Path(root) / 'MATLAB').glob('R20[0-9][0-9][ab]'), reverse=True):
                    candidate = release / 'bin' / 'matlab.exe'
                    if candidate.is_file():
                        return candidate.resolve()
    return None


def _cleanup_transfer(folder: Path):
    # Delete only the fixed transfer files that this module creates, no recursion.
    for name in ('data.mat', 'open_recording.m', 'status.json'):
        try:
            (folder / name).unlink(missing_ok=True)
        except OSError:
            pass
    try:
        folder.rmdir()
    except OSError:
        pass


def _start_matlab(arguments: list[str]) -> subprocess.Popen:
    environment = os.environ.copy()
    for variable in ('QT_QPA_PLATFORM', 'QT_PLUGIN_PATH', 'QT_QPA_PLATFORM_PLUGIN_PATH', 'QML2_IMPORT_PATH', 'QML_IMPORT_PATH'):
        environment.pop(variable, None)
    bundle = getattr(sys, '_MEIPASS', None) if getattr(sys, 'frozen', False) else None
    if bundle:
        prefix = os.path.normcase(os.path.abspath(bundle)) + os.sep
        environment['PATH'] = os.pathsep.join(
            entry for entry in environment.get('PATH', '').split(os.pathsep)
            if not (os.path.normcase(os.path.abspath(entry)) + os.sep).startswith(prefix))
        # Frozen Linux applications prepend their libraries to this variable.
        # MATLAB must use the system's original library search path instead.
        if 'LD_LIBRARY_PATH_ORIG' in environment:
            environment['LD_LIBRARY_PATH'] = environment['LD_LIBRARY_PATH_ORIG']
        else:
            environment.pop('LD_LIBRARY_PATH', None)
    kernel = None
    previous = None
    if os.name == 'nt' and bundle:
        # PyInstaller's DLL search directory is inherited by Windows children.
        # Temporarily restore system search while creating the external process.
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetDllDirectoryW.argtypes = (wintypes.DWORD, wintypes.LPWSTR)
        kernel.GetDllDirectoryW.restype = wintypes.DWORD
        kernel.SetDllDirectoryW.argtypes = (wintypes.LPCWSTR,)
        kernel.SetDllDirectoryW.restype = wintypes.BOOL
        length = kernel.GetDllDirectoryW(0, None)
        buffer = ctypes.create_unicode_buffer(length + 1)
        kernel.GetDllDirectoryW(len(buffer), buffer)
        previous = buffer.value or None
        if not kernel.SetDllDirectoryW(None):
            raise ctypes.WinError(ctypes.get_last_error())
    try:
        return subprocess.Popen(arguments, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, shell=False, cwd=tempfile.gettempdir(), env=environment,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    finally:
        if kernel:
            kernel.SetDllDirectoryW(previous)


@dataclass
class MatlabLaunch:
    folder: Path
    process: subprocess.Popen
    started: float = field(default_factory=time.monotonic)

    def result(self) -> dict | None:
        try:
            result = json.loads((self.folder / 'status.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            result = None
        if result is not None:
            _cleanup_transfer(self.folder)
            return result
        if self.process.poll() is not None:
            _cleanup_transfer(self.folder)
            raise RuntimeError('MATLAB 未完成导入便退出，请检查 MATLAB 启动及许可证状态')
        if time.monotonic() - self.started > STARTUP_TIMEOUT:
            # MATLAB may still be starting; leave its input until it has loaded it.
            raise TimeoutError('MATLAB 尚未完成导入，请检查其启动窗口及许可证状态')
        return None


def open_recording(reader: SegmentReader, start: float, end: float, channels: list[int], *, progress=None) -> MatlabLaunch:
    if not math.isfinite(start) or not math.isfinite(end) or end <= start:
        raise ValueError('终点必须大于起点')
    count = reader.profile.rx.channels
    if not channels:
        raise ValueError('至少选择一个通道')
    if len(set(channels)) != len(channels) or any(type(c) is not int or not 0 <= c < count for c in channels):
        raise ValueError('接收通道选择无效')
    rx = reader.profile.rx
    lo = max(0, min(reader.rows, round(start * rx.sample_rate)))
    hi = max(lo, min(reader.rows, round(end * rx.sample_rate)))
    if hi == lo:
        raise ValueError('所选区间没有数据')
    size = dtype_for(rx.data_type, reader.profile.serial.byte_order).itemsize
    if (hi - lo) * (count * (size + 8) + 9) > MAX_IMPORT_BYTES:
        raise ValueError('该 MATLAB 查看区间预计超过 128 MiB，请缩小时间区间；完整记录可使用导出 MAT')
    executable = find_matlab()
    if executable is None:
        raise FileNotFoundError('未找到 MATLAB，请确认已安装 MATLAB，并将其 bin 目录加入系统 PATH')
    folder = Path(tempfile.mkdtemp(prefix='sciscope-matlab-')).resolve()
    try:
        export_segment(reader, folder / 'data.mat', 'mat', start, end, channels, progress=progress)
        (folder / 'open_recording.m').write_text(MATLAB_SCRIPT, encoding='utf-8')
        script = str(folder / 'open_recording.m').replace('\\', '/').replace("'", "''")
        arguments = [str(executable), '-desktop']
        if os.name == 'nt':
            arguments.append('-wait')
        arguments.extend(['-r', f"run('{script}');"])
        process = _start_matlab(arguments)
        return MatlabLaunch(folder, process)
    except BaseException:
        _cleanup_transfer(folder)
        raise
