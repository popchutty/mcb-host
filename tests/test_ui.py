import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json
import time
from pathlib import Path

import numpy as np
import pytest
from PySide6 import QtCore, QtGui, QtTest, QtWidgets as W
from serial.tools.list_ports_common import ListPortInfo

from sciscope.ui import MainWindow, ScopeWidget, configure_application, fill_row
from sciscope.acquisition import DataHub
from sciscope.protocol import FrameParser, make_frame
from sciscope.storage import FrameRecord, SegmentReader, SessionRecorder, session_files


def pump(app, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.005)


@pytest.fixture
def window_app(tmp_path):
    app = W.QApplication.instance() or W.QApplication([])
    preferences = QtCore.QSettings(str(tmp_path / "display.ini"), QtCore.QSettings.Format.IniFormat)
    window = MainWindow(display_preferences=preferences)
    window.show()
    try:
        yield app, window
    finally:
        window.close()
        window.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        pump(app, 0.05)


@pytest.fixture
def enumerated_ports(request, monkeypatch):
    devices = []
    for device, description in request.param:
        info = ListPortInfo(device, skip_link_detection=True)
        info.description = description
        devices.append(info)
    monkeypatch.setattr("sciscope.ui.list_ports.comports", lambda: devices)
    return devices


@pytest.fixture
def ports_window(enumerated_ports, window_app):
    return *window_app, enumerated_ports


@pytest.mark.parametrize("enumerated_ports", [
    [("COM13", "蓝牙链接上的标准串行 (COM13)"), ("COM7", name), ("COM4", None), ("COM5", "n/a"), ("COM6", "COM6")]
    for name in ("XDS100 Class USB Serial Port (COM7)", "XDS 100 Class USB Serial (COM7)", "xds100 class usb serial")
], indirect=True)
def test_port_descriptions_and_xds100_startup_selection(ports_window):
    app, window, devices = ports_window
    assert [window.port.itemData(i) for i in range(window.port.count())] == ["COM4", "COM5", "COM6", "COM7", "COM13"]
    assert [window.port.itemText(i) for i in range(3)] == ["COM4", "COM5", "COM6"]
    assert window.port.currentData() == window.selected_port() == "COM7"
    assert window.port.currentText().startswith("COM7（") and "(COM7)" not in window.port.currentText()
    assert window.port.itemText(4) == "COM13（蓝牙链接上的标准串行）"
    assert window.port.toolTip() == window.port.currentText()
    assert window.port.lineEdit().cursorPosition() == 0
    assert window.worker is None and window.hub is None


@pytest.mark.parametrize("enumerated_ports", [
    [], [("COM13", "n/a")], [("COM13", "蓝牙串口 (COM13)"), ("COM4", "")]
], indirect=True)
def test_port_list_without_xds100_and_manual_entry(ports_window):
    app, window, devices = ports_window
    expected = sorted(devices)[0].device if devices else ""
    assert window.selected_port() == expected
    window.port.setCurrentText("COM42")
    window.refresh_ports()
    assert window.selected_port() == window.port.currentText() == "COM42"


@pytest.mark.parametrize("enumerated_ports", [[
    ("COM13", "蓝牙串口 (COM13)"), ("COM7", "XDS100 Class USB Serial Port (COM7)")
]], indirect=True)
def test_port_refresh_presets_and_json_use_device_not_label(tmp_path, ports_window):
    app, window, devices = ports_window
    window.port.setCurrentIndex(window.port.findData("COM13"))
    devices[0].description = "蓝牙串口新名称 (COM13)"
    window.refresh_ports()
    assert window.selected_port() == "COM13"
    assert window.port.currentText() == "COM13（蓝牙串口新名称）"
    for preset in (window.use_default_profile, window.use_foc_preset, window.use_dyno_preset):
        preset()
        assert window.selected_port() == "COM13"
        assert window.port.currentText() == "COM13（蓝牙串口新名称）"
        assert window.collect_profile().serial.port == "COM13"
    profile = window.profile.clone()
    profile.serial.port = "COM7"
    window.load_widgets(profile)
    assert window.port.currentText() == "COM7（XDS100 Class USB Serial Port）"
    window.collect_profile().save(tmp_path / "ports.json")
    assert json.loads((tmp_path / "ports.json").read_text(encoding="utf-8"))["serial"]["port"] == "COM7"
    window.port.setCurrentText("COM13")
    assert window.selected_port() == "COM13"
    window.use_default_profile()
    assert window.port.currentText() == "COM13（蓝牙串口新名称）"
    devices.clear()
    window.refresh_ports()
    assert window.port.currentText() == window.selected_port() == "COM13"


@pytest.mark.parametrize("enumerated_ports", [[
    ("COM7", "XDS100 Class USB Serial Port (COM7)")
]], indirect=True)
def test_connect_uses_raw_port_and_does_not_send_on_preselection(ports_window, monkeypatch):
    app, window, devices = ports_window
    opened, writes, closed = [], [], []
    class Port:
        in_waiting = 0
        def __init__(self, device, baudrate, **kwargs):
            opened.append((device, baudrate))
        def read(self, count):
            time.sleep(0.001)
            return b""
        def write(self, payload):
            writes.append(payload)
            return len(payload)
        def close(self):
            closed.append(True)
    monkeypatch.setattr("sciscope.acquisition.serial.Serial", Port)
    window.toggle_connection()
    pump(app, 0.08)
    assert opened == [("COM7", 12000000)]
    assert window.profile.serial.port == "COM7" and window.worker.isRunning()
    assert not writes
    window.disconnect()
    assert closed == [True]


def test_matlab_button_opens_selected_interval_in_background(tmp_path, window_app, monkeypatch):
    app, window = window_app
    assert not window.matlab_button.isEnabled()
    profile = window.profile.clone()
    profile.rx.sample_rate, profile.rx.samples_per_frame = 1000, 10
    recorder = SessionRecorder(tmp_path / 'matlab_session', profile)
    recorder.start()
    recorder.submit(FrameRecord(np.arange(40, dtype=np.float32).reshape(20, 2), time.time_ns(), 0, 0, False, profile))
    recorder.stop()
    recorder.join(5)
    window.open_session(recorder.root)
    pump(app, .1)
    window.history_start.setValue(.002)
    window.history_end.setValue(.015)
    window.history_scope.channels.item(1).setCheckState(QtCore.Qt.CheckState.Unchecked)
    messages, calls = [], []
    monkeypatch.setattr(window, 'notify', messages.append)
    class Launch:
        ready = False
        def result(self):
            return {'ok': True, 'viewer': 'sdi', 'signal_count': 1, 'samples': 13} if self.ready else None
    launch = Launch()
    def start(reader, start, end, channels, **kwargs):
        calls.append((reader.path, start, end, channels))
        return launch
    monkeypatch.setattr('sciscope.ui.open_matlab_recording', start)
    window.matlab_button.click()
    window.open_history_in_matlab()  # Repeated activation cannot launch a duplicate.
    pump(app, .1)
    assert calls == [(session_files(recorder.root)[0], .002, .015, [0])]
    assert window.matlab_pending and not window.matlab_button.isEnabled()
    launch.ready = True
    window.check_matlab_import()
    assert not window.matlab_pending and window.matlab_button.isEnabled()
    assert '仿真数据检查器' in window.history_message.text()
    assert not messages
    def missing(*args, **kwargs):
        raise FileNotFoundError('未找到 MATLAB')
    monkeypatch.setattr('sciscope.ui.open_matlab_recording', missing)
    window.matlab_button.click()
    pump(app, .1)
    assert messages == ['未找到 MATLAB']
    assert not window.matlab_pending and window.matlab_button.isEnabled()
    window.history_segment.addItem('missing.h5', str(tmp_path / 'missing.h5'))
    window.history_segment.setCurrentIndex(window.history_segment.count() - 1)
    assert window.history_reader is None and not window.matlab_button.isEnabled()


def test_ui_simulation_pause_record_config_and_shutdown(tmp_path, window_app):
    app, window = window_app
    window.simulation.setChecked(True)
    window.toggle_connection()
    window.tabs.setCurrentWidget(window.capture_page)
    pump(app, 0.4)
    assert window.hub.frames > 5
    assert len(window.live_scope.last_t) > 0
    recorder = window.hub.start_recording(tmp_path / "ui_session")
    window.live_scope.paused.setChecked(True)
    before = window.hub.frames
    last_plot = window.live_scope.last_t.copy()
    pump(app, 0.3)
    assert window.hub.frames > before
    np.testing.assert_array_equal(window.live_scope.last_t, last_plot)
    fill_row(window.tx_values, 0, ["Data1", "", "0.25"])
    fill_row(window.tx_values, 1, ["Data2", "", "80"])
    window.send_generic()
    pump(app, 0.1)
    assert [c.name for c in window.hub.profile.rx.channel_info] == ["CH1", "CH2"]
    assert window.hub.segment == 0
    window.live_scope.paused.setChecked(False)
    window.disconnect()
    pump(app, 0.1)
    recorder.join(5)
    assert recorder.written_rows > 0 and not recorder.failed
    assert window.worker is None
    window.open_session(tmp_path / "ui_session")
    pump(app, 0.2)
    assert window.history_cache is not None
    window.analyze_history()
    pump(app, 0.1)
    assert window.analysis_table.rowCount() == 2
    assert not window.analysis_table.isHidden()
    window.history_end.setValue(0.1)
    window.load_history_window()
    pump(app, 0.1)
    assert window.analysis_table.rowCount() == 0
    assert window.analysis_table.isHidden()
    window.tx_length.setValue(3)
    window.apply_config()
    assert window.profile.tx.data_length == 3


def test_receive_warning_is_transient_and_shared_across_tabs(window_app):
    app, window = window_app

    class ConnectedWorker:
        def isRunning(self):
            return True

    # No real port is opened. Exercise the same refresh path as a connected
    # device that stays silent, sends bad frames, then starts sending good ones.
    window.hub = DataHub(window.profile)
    window.worker = ConnectedWorker()
    try:
        assert window.connection_notice.isHidden()
        window.hub.started -= 4
        window.last_rate_time -= 0.6
        window.refresh()
        assert window.connection_notice.isVisible()
        assert "波特率" in window.connection_notice.text()
        window.tabs.setCurrentWidget(window.foc_page)
        app.processEvents()
        assert window.connection_notice.isVisible()
        warning = "疑似帧长不匹配，请检查通道数。"
        window.hub.protocol_warning = warning
        window.hub.resyncs = 1
        window.last_rate_time -= 0.6
        window.refresh()
        assert window.connection_notice.text() == warning
        assert "重同步 1 次" in window.status.text()
        window.hub.protocol_warning = ""
        frame = make_frame(window.profile, np.zeros((600, 2), dtype=np.float32))
        window.hub.accept(FrameParser(window.profile).feed(frame)[0])
        window.last_rate_time -= 0.6
        window.refresh()
        assert window.connection_notice.isHidden()
        # No extra baud-rate reference setting is introduced.
        assert set(window.profile.to_dict()["serial"]) == {"port", "baudrate", "bytesize", "parity", "stopbits", "byte_order"}
        window.hub.protocol_warning = warning
        window.last_rate_time -= 0.6
        window.refresh()
        assert window.connection_notice.isVisible()
        window.worker = None
        window.acquisition_finished()
        assert window.connection_notice.isHidden()
    finally:
        window.worker = None


def test_presets_and_optional_foc_tool_are_separate(window_app):
    app, window = window_app
    assert [window.tabs.tabText(i) for i in range(window.tabs.count())] == ["通用收发", "单电机 FOC", "对拖 FOC", "记录分析", "协议配置"]
    assert window.profile.tools == {}
    window.port.setCurrentText("COM42")
    window.use_foc_preset()
    tool = window.foc_tool
    assert not isinstance(tool, W.QDialog)
    assert not tool.start_button.isEnabled()
    assert window.port.currentText() == "COM42"
    assert window.tabs.currentWidget() is window.foc_page
    assert tool.map_channels.isChecked()
    window.simulation.setChecked(True)
    window.toggle_connection()
    sent = []
    window.worker.command_sent.connect(sent.append)
    pump(app, 0.1)
    assert not sent
    assert window.tabs.currentWidget() is window.foc_page
    assert tool.start_button.isEnabled()
    assert not tool.preset_row.isEnabled()
    assert not window.periodic.isActive()
    assert not window.profile.tools.get("foc", {}).get("enabled", False)
    before = window.hub.frames
    tool.send(True)
    pump(app, 0.1)
    assert window.hex_preview.toPlainText() == ""
    assert tool.hex_preview.toPlainText()
    tool.selection.setCurrentIndex(3)
    pump(app, 0.15)
    assert window.hub.frames > before
    assert window.hub.profile.rx.channel_info[0].name == "Speed_ref"
    tool.update_button.click()
    pump(app, 0.1)
    assert window.hub.profile.rx.channel_info[0].name == "Ia"
    tool.send(False)
    pump(app, 0.1)
    assert not tool.enabled
    window.disconnect()
    pump(app, 0.05)
    window.use_default_profile()
    assert [c.name for c in window.profile.rx.channel_info] == ["CH1", "CH2"]
    assert not tool.map_channels.isChecked()


@pytest.mark.parametrize("fixed", [False, True])
def test_foc_edits_wait_for_update_and_keep_enable_state(fixed, window_app):
    app, window = window_app
    window.use_foc_preset(fixed)
    window.simulation.setChecked(True)
    window.toggle_connection()
    pump(app, 0.1)
    tool = window.foc_tool
    sent = []
    window.worker.tagged_command_sent.connect(lambda origin, payload: sent.append((origin, payload)))

    def edit_parameters(rpm, selection):
        tool.n_base.setValue(4000)
        tool.n_base.editingFinished.emit()
        tool.i_base.setValue(20)
        tool.i_base.editingFinished.emit()
        tool.rpm.setValue(rpm)
        # Enter followed by focus loss can emit editingFinished twice.
        tool.rpm.editingFinished.emit()
        tool.rpm.editingFinished.emit()
        tool.selection.setCurrentIndex(selection)
        tool.map_channels.setChecked(False)
        tool.map_channels.setChecked(True)
        pump(app, 0.1)

    # Editing and updating before start must keep the enable bit clear.
    edit_parameters(1000, 1)
    assert not sent
    assert window.hub.profile.rx.channel_info[0].name == "Speed_ref"
    tool.update_button.click()
    pump(app, 0.1)
    dtype = "<u4" if fixed else "<f4"
    first = np.frombuffer(bytes.fromhex(sent[0][1]), dtype=dtype)
    assert sent[0][0] == "foc:update" and len(sent) == 1
    assert tool.message.text() == "更新命令已发送"
    assert first[0] == (1 << 26 if fixed else 0.25)
    assert first[1] == 32
    assert not tool.enabled
    assert window.hub.profile.rx.channel_info[0].name == "Id_ref"

    tool.start_button.click()
    pump(app, 0.1)
    assert sent[-1][0] == "foc:start" and tool.enabled
    count = len(sent)
    edit_parameters(2000, 3)
    assert len(sent) == count
    assert window.hub.profile.rx.channel_info[0].name == "Id_ref"
    tool.update_button.click()
    pump(app, 0.1)
    assert len(sent) == count + 1 and sent[-1][0] == "foc:update"
    assert tool.message.text() == "更新命令已发送"
    update = np.frombuffer(bytes.fromhex(sent[-1][1]), dtype=dtype)
    assert update[0] == (1 << 27 if fixed else 0.5)
    assert update[1] == 65 and tool.enabled
    assert window.hub.profile.rx.channel_info[0].name == "Ia"
    assert window.hub.profile.rx.channel_info[0].scale == 20

    tool.stop_button.click()
    pump(app, 0.1)
    assert sent[-1][0] == "foc:stop"
    stopped = np.frombuffer(bytes.fromhex(sent[-1][1]), dtype=dtype)
    assert stopped[1] == 64 and not tool.enabled
    count = len(sent)
    edit_parameters(1200, 2)
    assert len(sent) == count
    tool.update_button.click()
    pump(app, 0.1)
    assert len(sent) == count + 1 and sent[-1][0] == "foc:update"
    after_stop = np.frombuffer(bytes.fromhex(sent[-1][1]), dtype=dtype)
    assert after_stop[1] == 48 and not tool.enabled
    assert window.hex_preview.toPlainText() == ""


def test_custom_frame_applies_on_connect_without_overwriting_names(window_app):
    app, window = window_app
    window.tx_length.setValue(3)
    window.tx_type.setCurrentText("int16")
    window.rx_count.setValue(8)
    fill_row(window.rx_fields, 0, ["Pressure", "kPa", 0.25, 10, 0, 0])
    window.simulation.setChecked(True)
    window.toggle_connection()
    pump(app, 0.15)
    assert window.tx_values.rowCount() == 3
    assert window.hub.profile.rx.channels == 8
    assert window.hub.profile.rx.channel_info[0].name == "Pressure"
    fill_row(window.tx_values, 0, ["Data1", "", "12"])
    window.send_generic()
    pump(app, 0.1)
    assert window.hub.profile.rx.channel_info[0].scale == 0.25


def test_generic_send_stays_generic_even_under_foc_preset(window_app):
    app, window = window_app
    window.use_foc_preset()
    window.simulation.setChecked(True)
    window.toggle_connection()
    pump(app, 0.1)
    metadata_before = window.hub.profile.rx.channel_info.copy()
    fill_row(window.tx_values, 0, ["Speed_PU", "", "0.5"])
    fill_row(window.tx_values, 1, ["Control", "", "999"])
    sent = []
    window.worker.command_sent.connect(sent.append)
    window.send_generic()
    pump(app, 0.1)
    assert sent == ["00 00 00 3F 00 C0 79 44"]
    assert window.hub.profile.rx.channel_info == metadata_before
    assert window.hub.segment == 0
    assert window.foc_tool.hex_preview.toPlainText() == ""


def test_live_scope_and_recording_follow_navigation(tmp_path, monkeypatch, window_app):
    app, window = window_app
    window.simulation.setChecked(True)
    window.toggle_connection()
    pump(app, 0.2)
    scope = window.live_scope
    scope.paused.setChecked(True)
    frozen = scope.last_t.copy()
    limits = scope.plots[0].viewRange()[0]
    sent = []
    window.worker.command_sent.connect(sent.append)
    window.tabs.setCurrentWidget(window.foc_page)
    pump(app, 0.1)
    assert window.live_scope is scope
    assert scope.parentWidget() is window.foc_scope_host
    np.testing.assert_array_equal(scope.last_t, frozen)
    np.testing.assert_allclose(scope.plots[0].viewRange()[0], limits)
    assert scope.paused.isChecked()
    assert not sent and window.profile.tools == {}
    monkeypatch.setattr(W.QFileDialog, "getExistingDirectory", lambda *args: str(tmp_path))
    window.toggle_recording()
    recorder = window.hub.recorder
    pump(app, 0.15)
    window.tabs.setCurrentWidget(window.history_page)
    pump(app, 0.1)
    assert not window.history_scope.paused.isVisible()
    assert not window.history_scope.window.isVisible()
    window.toggle_recording()
    recorder.join(5)
    assert recorder.written_rows > 0 and not recorder.failed
    window.tabs.setCurrentWidget(window.capture_page)
    pump(app, 0.05)
    assert scope.parentWidget() is window.capture_scope_host
    np.testing.assert_array_equal(scope.last_t, frozen)
    scope.paused.setChecked(False)
    pump(app, 0.1)
    assert scope.last_t[-1] > frozen[-1]


def test_foc_preset_restores_q28_and_incompatible_controls_disable(window_app):
    app, window = window_app
    window.use_foc_preset(True)
    assert window.profile.tx.data_type == "uint32"
    assert window.foc_tool.preset.currentText() == "Q28"
    window.use_default_profile()
    window.tx_length.setValue(3)
    window.apply_config()
    window.simulation.setChecked(True)
    window.toggle_connection()
    pump(app, 0.1)
    assert not window.foc_tool.start_button.isEnabled()
    assert window.send_button.isEnabled()


@pytest.mark.parametrize("fixed", [False, True])
def test_dyno_explicit_updates_enable_state_mapping_and_feedback(tmp_path, fixed, window_app):
    app, window = window_app
    window.port.setCurrentText("COM42")
    window.use_dyno_preset(fixed)
    tool = window.dyno_tool
    assert window.tabs.currentWidget() is window.dyno_page
    assert window.port.currentText() == "COM42"
    assert tool.selection.count() == 16 and tool.selection.currentData() == 0
    for motor, widgets in tool.base_widgets.items():
        for key, widget in widgets.items():
            assert widget.value() == pytest.approx(window.profile.tools["dyno"][motor][key], rel=0, abs=1e-12)
    assert tool.map_channels.isChecked() and tool.base_grid.isVisible()
    assert not tool.start_button.isEnabled()
    window.simulation.setChecked(True)
    window.toggle_connection()
    sent = []
    window.worker.tagged_command_sent.connect(lambda origin, payload: sent.append((origin, payload)))
    recorder = window.hub.start_recording(tmp_path / "dyno_session")
    pump(app, 0.1)
    assert not sent and window.hub.frames > 0
    assert tool.start_button.isEnabled() and not window.foc_tool.start_button.isEnabled()
    assert not tool.preset_row.isEnabled()
    tool.base_widgets["motor1"]["n_base"].setValue(4096)
    tool.base_widgets["motor1"]["n_base"].editingFinished.emit()
    tool.base_widgets["motor2"]["i_base"].setValue(4)
    tool.base_widgets["motor2"]["i_base"].editingFinished.emit()
    tool.rpm.setValue(1024)
    tool.imag_ref.setValue(2)
    tool.imag_pos.setValue(270)
    for widget in (tool.rpm, tool.imag_ref, tool.imag_pos):
        widget.editingFinished.emit()
        widget.editingFinished.emit()
    tool.selection.setCurrentIndex(4)
    pump(app, 0.1)
    assert not sent and window.hub.profile.rx.channel_info[0].name == "M1_Speed_ref"
    tool.update_button.click()
    pump(app, 0.1)
    raw = np.frombuffer(bytes.fromhex(sent[0][1]), dtype="<u4" if fixed else "<f4")
    assert raw.tolist() == ([1 << 26, 64, 1 << 27, 3 << 26] if fixed else [0.25, 64, 0.5, 0.75])
    assert len(sent) == 1 and sent[0][0] == "dyno:update"
    assert not tool.enabled and tool.message.text() == "更新命令已发送"
    assert window.hub.profile.rx.channel_info[0].name == "M1_Vd"
    assert window.foc_tool.hex_preview.toPlainText() == window.hex_preview.toPlainText() == ""
    tool.start_button.click()
    pump(app, 0.1)
    assert tool.enabled and sent[-1][0] == "dyno:start"
    tool.selection.setCurrentIndex(11)
    tool.rpm.setValue(512)
    tool.rpm.editingFinished.emit()
    pump(app, 0.1)
    assert len(sent) == 2 and window.hub.profile.rx.channel_info[0].name == "M1_Vd"
    tool.update_button.click()
    pump(app, 0.1)
    assert len(sent) == 3 and tool.enabled
    assert np.frombuffer(bytes.fromhex(sent[-1][1]), dtype="<u4" if fixed else "<f4")[1] == 177
    assert [c.name for c in window.hub.profile.rx.channel_info] == ["M2_Pm", "M2_Te"]
    scope = window.live_scope
    scope.paused.setChecked(True)
    frozen = scope.last_t.copy()
    for page, host in [(window.capture_page, window.capture_scope_host), (window.foc_page, window.foc_scope_host),
                       (window.dyno_page, window.dyno_scope_host)]:
        window.tabs.setCurrentWidget(page)
        pump(app, 0.05)
        assert window.live_scope is scope and scope.parentWidget() is host
        np.testing.assert_array_equal(scope.last_t, frozen)
    assert len(sent) == 3
    tool.stop_button.click()
    pump(app, 0.1)
    assert len(sent) == 4 and sent[-1][0] == "dyno:stop" and not tool.enabled
    tool.update_button.click()
    pump(app, 0.1)
    assert np.frombuffer(bytes.fromhex(sent[-1][1]), dtype="<u4" if fixed else "<f4")[1] == 176
    window.disconnect()
    recorder.join(5)
    assert not recorder.failed and recorder.written_rows > 0
    import json
    events = [json.loads(line) for line in (recorder.root / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [e["origin"] for e in events if e["type"] == "tx"] == ["dyno:update", "dyno:start", "dyno:update", "dyno:stop", "dyno:update"]
    window.toggle_connection()
    pump(app, 0.1)
    assert not tool.enabled and not window.worker.commands.qsize()
    window.disconnect()


def test_dyno_extensible_receive_and_generic_frames_stay_independent(window_app):
    app, window = window_app
    window.use_dyno_preset()
    window.rx_count.setValue(8)
    window.apply_config()
    window.simulation.setChecked(True)
    window.toggle_connection()
    pump(app, 0.1)
    tool = window.dyno_tool
    assert not tool.start_button.isEnabled()
    tool.map_channels.setChecked(False)
    assert tool.start_button.isEnabled()
    metadata = window.hub.profile.rx.channel_info.copy()
    sent = []
    window.worker.tagged_command_sent.connect(lambda origin, payload: sent.append((origin, payload)))
    tool.update_button.click()
    pump(app, 0.1)
    assert window.hub.profile.rx.channel_info == metadata and window.hub.segment == 0
    for i, value in enumerate([0.5, 999, 0.25, 0.75]):
        fill_row(window.tx_values, i, [f"Data{i}", "", str(value)])
    window.send_generic()
    pump(app, 0.1)
    assert sent[-1][0] == "generic"
    assert np.frombuffer(bytes.fromhex(sent[-1][1]), dtype="<f4").tolist() == [0.5, 999, 0.25, 0.75]
    assert window.hub.profile.rx.channel_info == metadata
    assert "已发送" in window.hex_preview.toPlainText()


def test_model_sidebar_dragging_shares_width_without_commands(window_app):
    app, window = window_app
    configure_application(app)
    window.simulation.setChecked(True)
    window.toggle_connection()
    pump(app, 0.1)
    sent = []
    window.worker.command_sent.connect(sent.append)
    window.live_scope.paused.setChecked(True)
    frozen = window.live_scope.last_t.copy()
    before = window.hub.frames
    for page, sidebar, panel in [(window.foc_page, window.foc_sidebar, window.foc_tool),
                                  (window.dyno_page, window.dyno_sidebar, window.dyno_tool)]:
        window.tabs.setCurrentWidget(page)
        pump(app, 0.05)
        assert sidebar.width() == 480
        assert panel.parameter_group.title() == "电机参数与控制"
        assert not panel.parameter_group.isCheckable()
        assert panel.findChildren(W.QGroupBox) == [panel.parameter_group]
        for widgets in panel.base_widgets.values():
            for widget in widgets.values():
                assert widget.isVisible()
                assert widget.mapTo(panel, QtCore.QPoint()).y() < panel.rpm.mapTo(panel, QtCore.QPoint()).y()
        assert panel.rpm.mapTo(panel, QtCore.QPoint()).y() < panel.selection.mapTo(panel, QtCore.QPoint()).y()
        assert panel.selection.mapTo(panel, QtCore.QPoint()).y() < panel.update_button.mapTo(panel, QtCore.QPoint()).y()
        assert panel.backgroundRole() == QtGui.QPalette.ColorRole.Base
        # Check the actual rendered background below the control group.
        background = sidebar.viewport().grab().toImage().pixelColor(sidebar.viewport().width() - 4, sidebar.viewport().height() - 4)
        assert background == QtGui.QColor("#ffffff")

    def drag(splitter, distance):
        handle = splitter.handle(1)
        assert handle.isVisible() and handle.width() >= 10
        center = handle.rect().center()
        QtTest.QTest.mousePress(handle, QtCore.Qt.MouseButton.LeftButton, pos=center)
        QtTest.QTest.mouseMove(handle, center + QtCore.QPoint(distance, 0), delay=10)
        QtTest.QTest.mouseRelease(handle, QtCore.Qt.MouseButton.LeftButton, pos=center)
        pump(app, 0.05)

    drag(window.dyno_splitter, -80)
    wider = window.dyno_sidebar.width()
    assert wider == pytest.approx(560, abs=2)
    window.tabs.setCurrentWidget(window.foc_page)
    pump(app, 0.05)
    assert window.foc_sidebar.width() == wider
    drag(window.foc_splitter, 240)
    narrower = window.foc_sidebar.width()
    assert narrower == pytest.approx(320, abs=2)
    window.tabs.setCurrentWidget(window.dyno_page)
    pump(app, 0.05)
    assert window.dyno_sidebar.width() == narrower
    assert window.hub.frames > before and not sent
    assert window.profile.tools == {}
    np.testing.assert_array_equal(window.live_scope.last_t, frozen)


def test_both_model_sidebars_scroll_in_a_small_window(window_app):
    app, window = window_app
    configure_application(app)
    window.resize(1000, 600)
    window.model_sidebar_width = 320
    for page, sidebar, panel in [(window.foc_page, window.foc_sidebar, window.foc_tool),
                                  (window.dyno_page, window.dyno_sidebar, window.dyno_tool)]:
        window.tabs.setCurrentWidget(page)
        pump(app, 0.1)
        assert sidebar.width() >= 260
        for widgets in panel.base_widgets.values():
            for widget in widgets.values():
                sidebar.ensureWidgetVisible(widget)
                pump(app, 0.01)
                field_rect = QtCore.QRect(widget.mapTo(sidebar.viewport(), QtCore.QPoint()), widget.size())
                assert sidebar.viewport().rect().intersects(field_rect)
        sidebar.ensureWidgetVisible(panel.hex_preview)
        pump(app, 0.05)
        visible_rect = QtCore.QRect(panel.hex_preview.mapTo(sidebar.viewport(), QtCore.QPoint()), panel.hex_preview.size())
        assert sidebar.viewport().rect().intersects(visible_rect)
    assert window.dyno_sidebar.verticalScrollBar().maximum() > 0


def test_live_signal_sidebar_drag_fold_and_navigation_keep_acquiring(tmp_path, window_app):
    app, window = window_app
    configure_application(app)
    window.simulation.setChecked(True)
    window.toggle_connection()
    window.tabs.setCurrentWidget(window.capture_page)
    pump(app, 0.2)
    scope = window.live_scope
    assert scope.channel_pane.width() == 190
    scope.channels.item(1).setCheckState(QtCore.Qt.CheckState.Unchecked)
    pump(app, 0.05)
    curve = scope.curves[0]
    recorder = window.hub.start_recording(tmp_path / "folding_session")
    sent = []
    window.worker.command_sent.connect(sent.append)
    handle = scope.channel_splitter.handle(1)
    center = handle.rect().center()
    QtTest.QTest.mousePress(handle, QtCore.Qt.MouseButton.LeftButton, pos=center)
    QtTest.QTest.mouseMove(handle, center + QtCore.QPoint(100, 0), delay=10)
    QtTest.QTest.mouseRelease(handle, QtCore.Qt.MouseButton.LeftButton, pos=center)
    pump(app, 0.05)
    width = scope.channel_pane.width()
    assert width == pytest.approx(290, abs=2)
    plot_width = scope.graphics.width()
    last_time = scope.last_t[-1]
    before = window.hub.frames
    QtTest.QTest.mouseClick(scope.collapse_channels, QtCore.Qt.MouseButton.LeftButton)
    pump(app, 0.15)
    assert scope.channel_pane.isHidden() and scope.expand_channels.isVisible()
    assert not handle.isVisible()
    assert scope.graphics.width() > plot_width
    assert window.hub.frames > before and scope.last_t[-1] > last_time
    assert scope.selected() == [0] and scope.curves[0] is curve
    for page in (window.foc_page, window.dyno_page, window.capture_page):
        window.tabs.setCurrentWidget(page)
        pump(app, 0.05)
        assert scope.channel_pane.isHidden() and scope.expand_channels.isVisible()
        assert scope.channel_width == width
        assert scope.selected() == [0] and scope.curves[0] is curve
    window.resize(1200, 760)
    pump(app, 0.05)
    QtTest.QTest.mouseClick(scope.expand_channels, QtCore.Qt.MouseButton.LeftButton)
    pump(app, 0.05)
    assert scope.channel_pane.isVisible() and scope.channel_rail.isHidden()
    assert scope.channel_pane.width() == width and handle.isVisible()
    window.tabs.setCurrentWidget(window.dyno_page)
    pump(app, 0.05)
    assert scope.channel_pane.width() == width
    assert not sent
    window.disconnect()
    recorder.join(5)
    assert not recorder.failed and recorder.written_rows > 0
    from sciscope.storage import SegmentReader, session_files
    files = session_files(recorder.root)
    assert len(files) == 1
    assert SegmentReader(files[0]).profile.rx.channels == 2


def test_history_signal_sidebar_folds_without_changing_data_or_selection(tmp_path, window_app):
    app, window = window_app
    configure_application(app)
    profile = window.profile
    hub = DataHub(profile)
    recorder = hub.start_recording(tmp_path / "history_sidebar")
    values = np.arange(1200, dtype=np.float32).reshape(600, 2)
    parser = FrameParser(profile)
    for frame in parser.feed(make_frame(profile, values) * 3):
        hub.accept(frame)
    hub.stop_recording()
    recorder.join(5)
    window.open_session(recorder.root)
    window.tabs.setCurrentWidget(window.history_page)
    pump(app, 0.15)
    scope = window.history_scope
    assert scope.channel_pane.width() == 190
    times, data = scope.last_t.copy(), scope.last_data.copy()
    curves = dict(scope.curves)
    handle = scope.channel_splitter.handle(1)
    center = handle.rect().center()
    QtTest.QTest.mousePress(handle, QtCore.Qt.MouseButton.LeftButton, pos=center)
    QtTest.QTest.mouseMove(handle, center + QtCore.QPoint(60, 0), delay=10)
    QtTest.QTest.mouseRelease(handle, QtCore.Qt.MouseButton.LeftButton, pos=center)
    pump(app, 0.05)
    width = scope.channel_pane.width()
    assert width == pytest.approx(250, abs=2)
    QtTest.QTest.mouseClick(scope.collapse_channels, QtCore.Qt.MouseButton.LeftButton)
    pump(app, 0.05)
    assert scope.channel_pane.isHidden()
    assert scope.selected() == [0, 1] and scope.curves == curves
    np.testing.assert_array_equal(scope.last_t, times)
    np.testing.assert_array_equal(scope.last_data, data)
    window.tabs.setCurrentWidget(window.capture_page)
    pump(app, 0.05)
    assert not window.live_scope.channels_collapsed
    window.tabs.setCurrentWidget(window.history_page)
    pump(app, 0.05)
    assert scope.channel_pane.isHidden() and scope.expand_channels.isVisible()
    QtTest.QTest.mouseClick(scope.expand_channels, QtCore.Qt.MouseButton.LeftButton)
    pump(app, 0.05)
    assert scope.channel_pane.width() == width and scope.selected() == [0, 1]


def test_visible_history_stays_stable_when_offscreen_data_grows_and_zoom_reveals_samples(window_app):
    app, window = window_app
    scope = ScopeWidget(live=False)
    scope.configure(window.profile)
    scope.resize(1000, 650)
    scope.show()
    try:
        t = np.arange(260000) / 20000
        data = np.column_stack((np.sin(2 * np.pi * 13 * t), np.cos(2 * np.pi * 17 * t)))
        peak_index = 164691
        data[peak_index, 0] = 9
        data[230000, :] = 1000  # Outside the viewed historical interval.
        scope.update_data(t[:200000], data[:200000])
        pump(app, 0.08)
        scope.plots[0].setXRange(8, 9, padding=0)
        pump(app, 0.1)
        before = {i: tuple(a.copy() for a in curve.getData()) for i, curve in scope.curves.items()}
        limits = scope.plots[0].viewRange()
        scope.update_data(t, data)
        pump(app, 0.1)
        for i, curve in scope.curves.items():
            for actual, expected in zip(curve.getData(), before[i]):
                np.testing.assert_array_equal(actual, expected)
        np.testing.assert_allclose(scope.plots[0].viewRange(), limits)
        scope.plots[0].setXRange(8.234, 8.236, padding=0)
        pump(app, 0.1)
        x, y = scope.curves[0].getData()
        assert len(x) <= 43  # Zoom uses the original samples, not a coarse old envelope.
        assert x[y == 9].tolist() == [t[peak_index]]
        np.testing.assert_array_equal(scope.last_data, data)
    finally:
        scope.close()
        scope.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)


@pytest.mark.parametrize("channels", [8, 14])
def test_live_plot_has_only_pause_skips_idle_copies_and_keeps_recording(tmp_path, monkeypatch, channels, window_app):
    app, window = window_app
    window.rx_count.setValue(channels)
    window.apply_config()
    hub = DataHub(window.profile)
    window.hub = hub
    window.tabs.setCurrentWidget(window.capture_page)
    scope = window.live_scope
    assert all(widget.text() != "跟随最新" for widget in window.findChildren(W.QCheckBox))
    recorder = hub.start_recording(tmp_path / f"plot_{channels}ch")
    parser = FrameParser(window.profile)
    n = window.profile.rx.samples_per_frame
    source = np.arange(n * 87 * channels, dtype=np.float32).reshape(n * 87, channels)
    try:
        def accept_frames(begin, end):
            for row in range(begin, end, n):
                for frame in parser.feed(make_frame(window.profile, source[row:row + n])):
                    hub.accept(frame)

        accept_frames(0, n * 67)
        copies = []
        snapshot = hub.ring.snapshot_range

        def counted_snapshot(begin, end):
            copies.append((begin, end))
            return snapshot(begin, end)

        monkeypatch.setattr(hub.ring, "snapshot_range", counted_snapshot)
        window.refresh()
        pump(app, 0.15)
        assert len(copies) == 1 and copies[0][1] - copies[0][0] <= 20002
        for _ in range(10):
            window.refresh()
        assert len(copies) == 1
        assert len(scope.curves) == channels
        assert all(len(curve.getData()[0]) <= 4100 for curve in scope.curves.values())
        scope.paused.setChecked(True)
        frozen_t, frozen_data = scope.last_t.copy(), scope.last_data.copy()
        frozen_draw = {i: tuple(a.copy() for a in curve.getData()) for i, curve in scope.curves.items()}
        limits = scope.plots[0].viewRange()[0]
        accept_frames(n * 67, n * 87)
        window.refresh()
        pump(app, 0.1)
        assert hub.frames == 87 and len(copies) == 2
        np.testing.assert_array_equal(scope.last_t, frozen_t)
        np.testing.assert_array_equal(scope.last_data, frozen_data)
        np.testing.assert_allclose(scope.plots[0].viewRange()[0], limits)
        for i, curve in scope.curves.items():
            for actual, expected in zip(curve.getData(), frozen_draw[i]):
                np.testing.assert_array_equal(actual, expected)
        scope.plots[0].setXRange(frozen_t[-1] - 0.01, frozen_t[-1], padding=0)
        pump(app, 0.08)
        assert all(len(curve.getData()[0]) <= 203 for curve in scope.curves.values())
        scope.mode.setCurrentText("分图")
        pump(app, 0.1)
        assert len(scope.plots) == channels
        assert all(len(curve.getData()[0]) for curve in scope.curves.values())
        np.testing.assert_array_equal(scope.last_data, frozen_data)
        scope.channels.item(1).setCheckState(QtCore.Qt.CheckState.Unchecked)
        pump(app, 0.05)
        assert len(scope.curves) == channels - 1
        scope.paused.setChecked(False)
        window.refresh()
        pump(app, 0.1)
        assert scope.last_t[-1] > frozen_t[-1]
        assert scope.plots[0].viewRange()[0][1] == pytest.approx((n * 87 - 1) / 20000)
        assert len(copies) == 3
    finally:
        hub.stop_recording()
        recorder.join(5)
    assert not recorder.failed and recorder.written_rows == len(source)
    import h5py
    files = sorted(recorder.root.glob("segment_*.h5"))
    assert len(files) == 1
    with h5py.File(files[0], "r") as handle:
        np.testing.assert_array_equal(handle["raw"][:], source)


def test_metadata_is_always_editable_and_scope_controls_share_one_row(tmp_path, window_app):
    app, window = window_app
    configure_application(app)
    window.tabs.setCurrentWidget(window.config_page)
    pump(app, 0.05)
    groups = window.config_page.findChildren(W.QGroupBox)
    assert all(group.title() != "SCI Receive 参考参数" for group in groups)
    metadata = next(group for group in groups if group.title() == "字段名称与换算")
    assert not metadata.isCheckable()
    assert window.tx_fields.isVisible() and window.rx_fields.isVisible()
    assert window.tx_fields.isEnabled() and window.rx_fields.isEnabled()
    fill_row(window.tx_fields, 0, ["Cmd", "V", 2, 1])
    fill_row(window.rx_fields, 0, ["Voltage", "V", 0.5, 3, 0, 0])
    window.apply_config()
    assert window.profile.tx.fields[0].name == "Cmd"
    assert window.profile.rx.channel_info[0].scale == 0.5
    window.collect_profile().save(tmp_path / "new.json")
    import json
    assert "reference" not in json.loads((tmp_path / "new.json").read_text(encoding="utf-8"))["tx"]
    scope = window.live_scope
    window.tabs.setCurrentWidget(window.foc_page)
    pump(app, 0.08)
    controls = [scope.mode, scope.window, scope.buffer_points, scope.autorange_button,
                scope.paused, scope.units, scope.cursor_enabled]
    assert all(widget.parentWidget() is scope.controls_row for widget in controls)
    centers = [widget.geometry().center().y() for widget in controls]
    assert max(centers) - min(centers) <= 2
    assert all(left.geometry().right() < right.geometry().left() for left, right in zip(controls, controls[1:]))
    assert scope.units.geometry().left() - scope.paused.geometry().right() <= 12
    assert scope.cursor_enabled.geometry().left() - scope.units.geometry().right() <= 12
    window.resize(1000, 600)
    pump(app, 0.08)
    scope.controls_scroll.ensureWidgetVisible(scope.cursor_enabled)
    pump(app, 0.05)
    center = scope.cursor_enabled.mapTo(scope.controls_scroll.viewport(), scope.cursor_enabled.rect().center())
    assert scope.controls_scroll.viewport().rect().contains(center)
    QtTest.QTest.mouseClick(scope.cursor_enabled, QtCore.Qt.MouseButton.LeftButton)
    assert scope.cursor_enabled.isChecked()


@pytest.mark.parametrize("data_type,fractional,signed", [
    ("single", False, False), ("int8", True, False), ("uint8", True, True),
    ("int16", True, False), ("uint16", True, True), ("int32", True, False), ("uint32", True, True),
])
def test_rx_conversion_fields_follow_type_and_new_channels(window_app, data_type, fractional, signed):
    app, window = window_app
    window.rx_type.setCurrentText(data_type)
    window.rx_count.setValue(4)
    for row in range(4):
        for col, enabled in ((4, fractional), (5, signed)):
            flags = window.rx_fields.item(row, col).flags()
            assert bool(flags & QtCore.Qt.ItemFlag.ItemIsEnabled) == enabled
            assert bool(flags & QtCore.Qt.ItemFlag.ItemIsEditable) == enabled
            assert window.rx_fields.item(row, col).text() == "0"
            assert (window.rx_fields.item(row, col).data(QtCore.Qt.ItemDataRole.BackgroundRole) is None) == enabled
        for col in range(4):
            assert window.rx_fields.item(row, col).flags() & QtCore.Qt.ItemFlag.ItemIsEditable


def test_rx_type_changes_clear_inapplicable_values_and_save_consistently(tmp_path, window_app):
    from sciscope.config import Profile
    from sciscope.motor import official_profile

    app, window = window_app
    fixed = official_profile(True)
    window.load_widgets(fixed)
    assert window.rx_fields.item(0, 4).text() == "28"
    assert window.rx_fields.item(0, 5).text() == "1"
    window.rx_type.setCurrentText("int32")
    assert window.rx_fields.item(0, 4).text() == "28"
    assert window.rx_fields.item(0, 5).text() == "0"
    window.rx_type.setCurrentText("single")
    window.apply_config()
    path = tmp_path / "float.json"
    window.collect_profile().save(path)
    restored = Profile.load(path)
    assert restored.rx.data_type == "single"
    assert all(c.fractional_bits == 0 and not c.signed_bits for c in restored.rx.channel_info)
    assert all(c.scale == before.scale and c.offset == before.offset for c, before in zip(restored.rx.channel_info, fixed.rx.channel_info))
    # Loading an existing float configuration also clears inactive settings;
    # the caller's configuration and the official Q28 preset remain intact.
    stale = fixed.clone()
    stale.rx.data_type = "single"
    window.load_widgets(stale)
    assert all(c.fractional_bits == 0 and not c.signed_bits for c in window.profile.rx.channel_info)
    assert all(c.fractional_bits == 28 and c.signed_bits for c in stale.rx.channel_info)
    window.load_widgets(fixed)
    assert all(c.fractional_bits == 28 and c.signed_bits for c in window.collect_profile().rx.channel_info)
    assert window.rx_fields.item(0, 4).flags() & QtCore.Qt.ItemFlag.ItemIsEditable
    assert window.rx_fields.item(0, 5).flags() & QtCore.Qt.ItemFlag.ItemIsEditable
    assert window.rx_fields.item(0, 4).data(QtCore.Qt.ItemDataRole.BackgroundRole) is None
    assert window.rx_fields.item(0, 5).data(QtCore.Qt.ItemDataRole.ForegroundRole) is None


def test_optional_cursors_hide_lines_readouts_and_preserve_data(window_app):
    app, window = window_app
    p = window.profile.clone()
    p.rx.sample_rate, p.rx.samples_per_frame = 1000, 100
    t = np.arange(1000) / 1000
    data = np.column_stack((2 * t, 3 * t))
    for page, scope in ((window.capture_page, window.live_scope), (window.history_page, window.history_scope)):
        window.tabs.setCurrentWidget(page)
        scope.configure(p)
        scope.update_data(t, data)
        pump(app, 0.06)
        assert not scope.cursor_enabled.isChecked()
        assert not scope.cursor_a.isVisible() and not scope.cursor_b.isVisible()
        assert scope.cursor_label.isHidden() and not scope.cursor_label.text()
        scope.cursor_enabled.setChecked(True)
        scope.cursor_a.setValue(0.1)
        scope.cursor_b.setValue(0.2)
        assert scope.cursor_a.isVisible() and scope.cursor_b.isVisible()
        assert "Δt=0.1s" in scope.cursor_label.text() and "Δy=0.2" in scope.cursor_label.text()
        scope.mode.setCurrentText("分图")
        pump(app, 0.05)
        assert scope.cursor_a.isVisible() and scope.cursor_b.isVisible()
        scope.cursor_enabled.setChecked(False)
        assert not scope.cursor_a.isVisible() and not scope.cursor_b.isVisible()
        assert scope.cursor_label.isHidden() and not scope.cursor_label.text()
        np.testing.assert_array_equal(scope.last_t, t)
        np.testing.assert_array_equal(scope.last_data, data)
        scope.plots[0].setXRange(0.5, 0.6, padding=0)
        scope.cursor_enabled.setChecked(True)
        assert 0.5 <= scope.cursor_a.value() <= 0.6 and 0.5 <= scope.cursor_b.value() <= 0.6


def test_scope_depth_is_independent_saved_and_adjustable_while_connected(tmp_path, window_app):
    app, window = window_app
    scope = window.live_scope
    assert scope.isAncestorOf(scope.buffer_points)
    assert not window.config_page.isAncestorOf(scope.buffer_points)
    assert not hasattr(window.history_scope, "buffer_points")
    protocol = window.collect_profile().to_dict()
    scope.buffer_points.setValue(12345)
    assert window.collect_profile().to_dict() == protocol
    assert "0.617" in scope.buffer_points.toolTip()
    assert window.display_preferences.value("buffer_points", type=int) == 12345
    window.profile.save(tmp_path / "protocol.json")
    from sciscope.config import Profile
    restored = Profile.load(tmp_path / "protocol.json")
    assert "display" not in restored.to_dict()
    window.load_widgets(restored)
    assert scope.buffer_points.value() == 12345
    for preset in (window.use_foc_preset, window.use_dyno_preset, window.use_default_profile):
        preset()
        assert scope.buffer_points.value() == 12345
        assert "display" not in window.collect_profile().to_dict()
    window.simulation.setChecked(True)
    window.toggle_connection()
    pump(app, 0.1)
    assert window.hub.ring.capacity == 12345
    assert scope.buffer_points.isEnabled()
    assert scope.window.maximum() == pytest.approx(12345 / 20000)
    worker, hub = window.worker, window.hub
    before = hub.frames
    scope.buffer_points.setValue(25000)
    pump(app, 0.1)
    assert hub.ring.capacity == 25000 and hub.frames > before
    assert window.worker is worker and window.hub is hub
    assert hub.segment == hub.resyncs == 0 and worker.commands.empty()
    scope.buffer_points.setValue(50)
    pump(app, 0.08)
    assert hub.ring.count == hub.ring.capacity == 50
    window.disconnect()
    assert scope.buffer_points.isEnabled()
    second = MainWindow(display_preferences=window.display_preferences)
    try:
        assert second.live_scope.buffer_points.value() == 50
    finally:
        second.close()
        second.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        pump(app, 0.05)


def test_pause_preserves_full_ring_after_overwrite_and_metadata_changes(tmp_path, window_app):
    app, window = window_app
    p = window.profile.clone()
    p.rx.sample_rate, p.rx.samples_per_frame = 1000, 100
    p.rx.channel_info[0].name = "Old_signal"
    p.rx.channel_info[0].scale, p.rx.channel_info[0].offset = 2, 1
    window.load_widgets(p)
    window.live_scope.buffer_points.setValue(3000)
    window.tabs.setCurrentWidget(window.capture_page)
    hub = DataHub(p, buffer_points=3000)
    window.hub = hub
    scope = window.live_scope
    parser = FrameParser(p)
    source = np.column_stack((np.arange(11500), np.arange(11500) + 0.5)).astype(np.float32)
    recorder = hub.start_recording(tmp_path / "paused_buffer")
    try:
        def feed(begin, end):
            for row in range(begin, end, 100):
                for frame in parser.feed(make_frame(p, source[row:row + 100])):
                    hub.accept(frame)

        feed(0, 5000)
        window.refresh()
        pump(app, 0.08)
        assert len(scope.last_t) == 1002
        visible = scope.plots[0].viewRange()[0]
        scope.paused.setChecked(True)
        pump(app, 0.08)
        assert len(scope.last_t) == 3000
        assert scope.last_t[0] == 2 and scope.last_t[-1] == 4.999
        assert scope.cache_readout.isVisible() and "3,000 点/通道" in scope.cache_readout.text()
        np.testing.assert_allclose(scope.plots[0].viewRange()[0], visible)
        expected = source[2000:5000].astype(np.float64)
        expected[:, 0] = expected[:, 0] * 2 + 1
        np.testing.assert_array_equal(scope.last_data, expected)
        scope.window.setValue(3)
        pump(app, 0.08)
        np.testing.assert_allclose(scope.plots[0].viewRange()[0], [2, 4.999])
        np.testing.assert_array_equal(scope.last_data, expected)
        scope.window.setValue(1)
        frozen = scope.last_data.copy()
        scope.buffer_points.setValue(100)
        assert scope.window.maximum() == 3
        np.testing.assert_array_equal(scope.last_data, frozen)
        feed(5000, 11000)
        window.refresh()
        pump(app, 0.08)
        assert hub.ring.bounds() == (10900, 11000)
        np.testing.assert_array_equal(scope.last_data, frozen)
        scope.plots[0].setXRange(2.0, 2.2, padding=0)
        pump(app, 0.08)
        x, y = scope.curves[0].getData()
        assert x[0] == 2 and y[0] == source[2000, 0] * 2 + 1
        changed = p.clone()
        changed.rx.channel_info[0].name = "New_signal"
        changed.rx.channel_info[0].scale, changed.rx.channel_info[0].offset = 3, 10
        hub.update_metadata(changed)
        feed(11000, 11500)
        window.refresh()
        assert scope.profile.rx.channel_info[0].name == "Old_signal"
        np.testing.assert_array_equal(scope.last_data, frozen)
        window.si_units.setChecked(False)
        pump(app, 0.08)
        np.testing.assert_array_equal(scope.last_data, source[2000:5000])
        window.si_units.setChecked(True)
        pump(app, 0.08)
        np.testing.assert_array_equal(scope.last_data, frozen)
        scope.paused.setChecked(False)
        pump(app, 0.08)
        assert window.paused_snapshot is None
        assert scope.window.maximum() == 0.1
        assert scope.cache_readout.isHidden()
        assert scope.profile.rx.channel_info[0].name == "New_signal"
        assert scope.last_t[-1] == 0.499
        assert scope.last_data[-1, 0] == source[-1, 0] * 3 + 10
    finally:
        hub.stop_recording()
        recorder.join(5)
    assert not recorder.failed and recorder.written_rows == len(source)
    import h5py
    values = []
    for file in sorted(recorder.root.glob("segment_*.h5")):
        with h5py.File(file, "r") as handle:
            values.append(handle["raw"][:])
    np.testing.assert_array_equal(np.concatenate(values), source)
