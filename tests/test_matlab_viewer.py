import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import time

import numpy as np
import pytest
from scipy.io import loadmat

from sciscope.config import Channel, Profile
from sciscope.storage import FrameRecord, SegmentReader, SessionRecorder, session_files
from sciscope import matlab_viewer as viewer


@pytest.fixture
def recording(tmp_path):
    profile = Profile()
    profile.rx.sample_rate = 1000
    profile.rx.samples_per_frame = 10
    profile.rx.channels = 3
    profile.rx.channel_info = [Channel('速度', 'RPM', 2, 1), Channel("O'Brien", 'A'), Channel('电压', 'V', -3, 4)]
    recorder = SessionRecorder(tmp_path / '记录', profile)
    recorder.start()
    raw = np.arange(90, dtype=np.float32).reshape(30, 3)
    for index in range(3):
        recorder.submit(FrameRecord(raw[index * 10:index * 10 + 10], time.time_ns(), index * 10, 0, index == 0, profile))
    recorder.stop()
    recorder.join(5)
    assert not recorder.failed
    return SegmentReader(session_files(recorder.root)[0]), raw


def test_transfer_respects_selection_conversion_validity_and_source(recording, tmp_path, monkeypatch):
    reader, raw = recording
    original = hashlib.sha256(reader.path.read_bytes()).digest()
    folder = tmp_path / "包含空格 O'Brien"
    folder.mkdir()
    monkeypatch.setattr(viewer.tempfile, 'mkdtemp', lambda **kw: str(folder))
    monkeypatch.setattr(viewer, 'find_matlab', lambda: Path('matlab.exe'))
    launch_arguments = []
    process = SimpleNamespace(poll=lambda: None)
    monkeypatch.setattr(viewer, '_start_matlab', lambda arguments: launch_arguments.append(arguments) or process)
    launch = viewer.open_recording(reader, .005, .025, [2, 0])
    result = loadmat(folder / 'data.mat')
    expected = np.column_stack((raw[5:25, 2] * -3 + 4, raw[5:25, 0] * 2 + 1))
    np.testing.assert_array_equal(result['data'], expected)
    np.testing.assert_allclose(result['time_s'][:, 0], np.arange(5, 25) / 1000)
    np.testing.assert_array_equal(result['valid'][:, 0], [False] * 5 + [True] * 15)
    assert result['channel_names'][0, 0][0] == '电压'
    assert result['units'][0, 1][0] == 'RPM'
    assert "O''Brien" in launch_arguments[0][-1]
    assert '-desktop' in launch_arguments[0] and '-r' in launch_arguments[0]
    assert hashlib.sha256(reader.path.read_bytes()).digest() == original
    assert launch.result() is None
    (folder / 'status.json').write_text('{', encoding='utf-8')
    assert launch.result() is None  # A partially written status is retried.
    (folder / 'status.json').write_text(json.dumps({'ok': True}), encoding='utf-8')
    assert launch.result() == {'ok': True}
    assert not folder.exists()


@pytest.mark.parametrize('start,end,channels,message', [
    (0, 0, [0], '终点'), (0, .1, [], '通道'), (0, .1, [3], '通道'),
    (0, .1, [0, 0], '通道'), (1, 2, [0], '没有数据'), (0, float('nan'), [0], '终点')])
def test_invalid_selection_does_not_launch(recording, monkeypatch, start, end, channels, message):
    reader, _ = recording
    monkeypatch.setattr(viewer, 'find_matlab', lambda: pytest.fail('Invalid data must not launch MATLAB'))
    with pytest.raises(ValueError, match=message):
        viewer.open_recording(reader, start, end, channels)


def test_missing_matlab_and_bounded_import(recording, monkeypatch):
    reader, _ = recording
    monkeypatch.setattr(viewer, 'find_matlab', lambda: None)
    with pytest.raises(FileNotFoundError, match='未找到 MATLAB'):
        viewer.open_recording(reader, 0, .01, [0])
    reader.rows = 10**9
    with pytest.raises(ValueError, match='128 MiB'):
        viewer.open_recording(reader, 0, 1e6, [0])


def test_start_failure_cleans_transfer(recording, tmp_path, monkeypatch):
    reader, _ = recording
    folder = tmp_path / 'transfer'
    folder.mkdir()
    monkeypatch.setattr(viewer.tempfile, 'mkdtemp', lambda **kw: str(folder))
    monkeypatch.setattr(viewer, 'find_matlab', lambda: Path('matlab.exe'))
    def fail(arguments):
        raise OSError('Could not start MATLAB')
    monkeypatch.setattr(viewer, '_start_matlab', fail)
    with pytest.raises(OSError):
        viewer.open_recording(reader, 0, .01, [0])
    assert not folder.exists()


def test_process_exit_and_timeout(tmp_path):
    folder = tmp_path / 'transfer'
    folder.mkdir()
    (folder / 'data.mat').write_bytes(b'input')
    launch = viewer.MatlabLaunch(folder, SimpleNamespace(poll=lambda: 1))
    with pytest.raises(RuntimeError, match='退出'):
        launch.result()
    assert not folder.exists()
    folder.mkdir()
    (folder / 'data.mat').write_bytes(b'input')
    launch = viewer.MatlabLaunch(folder, SimpleNamespace(poll=lambda: None), started=time.monotonic() - 301)
    with pytest.raises(TimeoutError):
        launch.result()
    assert (folder / 'data.mat').exists()  # A slow MATLAB can still load its input.


def test_external_matlab_does_not_inherit_scope_qt_environment(monkeypatch):
    monkeypatch.setenv('QT_QPA_PLATFORM', 'offscreen')
    monkeypatch.setenv('QT_PLUGIN_PATH', 'scope/plugins')
    calls = []
    monkeypatch.setattr(viewer.subprocess, 'Popen', lambda arguments, **kw: calls.append(kw))
    viewer._start_matlab(['matlab.exe', '-desktop'])
    assert not calls[0]['shell']
    assert 'QT_QPA_PLATFORM' not in calls[0]['env'] and 'QT_PLUGIN_PATH' not in calls[0]['env']
    assert viewer.os.environ['QT_QPA_PLATFORM'] == 'offscreen'


def test_matlab_discovery_from_path(tmp_path, monkeypatch):
    executable = tmp_path / 'MATLAB' / 'bin' / 'matlab.exe'
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b'')
    monkeypatch.setattr(viewer.shutil, 'which', lambda name: str(executable))
    assert viewer.find_matlab() == executable.resolve()


@pytest.mark.parametrize("original", [None, "/usr/local/lib:/usr/lib"])
def test_external_matlab_restores_linux_library_environment(tmp_path, monkeypatch, original):
    monkeypatch.setattr(viewer.sys, "frozen", True, raising=False)
    monkeypatch.setattr(viewer.sys, "_MEIPASS", str(tmp_path), raising=False)
    monkeypatch.setenv("LD_LIBRARY_PATH", str(tmp_path))
    if original is None:
        monkeypatch.delenv("LD_LIBRARY_PATH_ORIG", raising=False)
    else:
        monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", original)
    calls = []
    monkeypatch.setattr(viewer.subprocess, "Popen", lambda arguments, **kwargs: calls.append(kwargs))
    viewer._start_matlab(["matlab", "-desktop"])
    assert calls[0]["env"].get("LD_LIBRARY_PATH") == original
    assert viewer.os.environ["LD_LIBRARY_PATH"] == str(tmp_path)


def test_matlab_discovery_from_registered_install(tmp_path, monkeypatch):
    executable = tmp_path / 'matlab.exe'
    executable.write_bytes(b'')
    monkeypatch.setattr(viewer.shutil, 'which', lambda name: None)
    monkeypatch.setattr(viewer, '_registry_executables', lambda: iter([tmp_path / 'missing.exe', executable]))
    assert viewer.find_matlab() == executable.resolve()
