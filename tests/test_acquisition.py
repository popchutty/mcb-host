import queue

import numpy as np
import pytest

from sciscope.acquisition import AcquisitionWorker, DataHub
from sciscope.config import Profile
from sciscope.protocol import FrameParser, make_frame, encode_values
from sciscope.storage import SegmentReader, session_files


class FakeSerial:
    def __init__(self):
        self.writes = []

    def write(self, payload):
        self.writes.append(payload)
        return len(payload)


@pytest.mark.parametrize("wire", [b"", b"unframed bytes"])
def test_no_valid_data_warns_without_claiming_baud_error(wire):
    hub = DataHub(Profile())
    hub.bytes_received = len(wire)
    assert not hub.reception_warning(hub.started + 2.9)
    assert "串口连接、波特率及接收协议配置" in hub.reception_warning(hub.started + 3.0)
    assert hub.resyncs == 0
    frame = FrameParser(hub.profile).feed(make_frame(hub.profile, np.zeros((600, 2), dtype=np.float32)))[0]
    hub.accept(frame)
    assert not hub.reception_warning(hub.started + 30)


def test_slow_protocol_waits_several_frame_periods_before_warning():
    p = Profile()
    p.rx.sample_rate = 100
    hub = DataHub(p)
    assert not hub.reception_warning(hub.started + 17.9)
    assert hub.reception_warning(hub.started + 18)


@pytest.mark.parametrize("wire_kind", ["silence", "garbage", "wrong_length"])
def test_worker_checks_receive_health_without_new_serial_configuration(monkeypatch, wire_kind):
    import sciscope.acquisition as acquisition

    p = Profile()
    p.serial.port = "COM_TEST"
    target = p.clone()
    target.rx.channels = 8
    wrong_frame = make_frame(target, np.zeros((600, 8), dtype=np.float32))
    wire = {"silence": b"", "garbage": b"noise" * 100,
            "wrong_length": wrong_frame * 4}[wire_kind]
    hub = DataHub(p)
    worker = AcquisitionWorker(p, hub)
    opened = []

    class ReadSerial(FakeSerial):
        def __init__(self, *args, **kwargs):
            super().__init__()
            self.wire = wire
            self.closed = False
            opened.append((args, kwargs, self))

        @property
        def in_waiting(self):
            return len(self.wire)

        def read(self, count):
            chunk, self.wire = self.wire[:count], self.wire[count:]
            if not self.wire:
                worker.stop()
            return chunk

        def close(self):
            self.closed = True

    monkeypatch.setattr(acquisition.serial, "Serial", ReadSerial)
    hub.started -= 100  # The timeout starts after opening, not before it.
    before_open = hub.started
    worker.run()
    assert hub.frames == 0 and hub.bytes_received == len(wire)
    assert hub.started >= before_open + 99
    if wire_kind == "wrong_length":
        assert hub.protocol_warning and "帧长不匹配" in hub.reception_warning(hub.started + 0.1)
    else:
        assert not hub.reception_warning(hub.started + 0.1)
        assert "波特率" in hub.reception_warning(hub.started + 4)
    assert hub.resyncs == (1 if wire_kind == "wrong_length" else 0)
    assert opened[0][0] == ("COM_TEST", p.serial.baudrate)
    assert opened[0][2].closed


def test_explicit_urgent_action_discards_pending_updates():
    p = Profile()
    worker = AcquisitionWorker(p, DataHub(p))
    port = FakeSerial()
    worker.send(encode_values(p, [1000, 1]))
    stop = encode_values(p, [0, 0])
    worker.send(stop, urgent=True)
    worker._commands(port)
    assert port.writes == [stop]
    assert worker.commands.empty()


def test_latest_urgent_update_is_not_lost():
    p = Profile()
    worker = AcquisitionWorker(p, DataHub(p))
    port = FakeSerial()
    worker.send(encode_values(p, [1000, 0]), urgent=True)
    latest = encode_values(p, [2000, 5])
    worker.send(latest, urgent=True)
    worker._commands(port)
    assert port.writes == [latest]
    assert worker.hub.profile.to_dict() == p.to_dict()


def test_explicit_metadata_change_and_resync_create_record_segments(tmp_path):
    p = Profile()
    hub = DataHub(p)
    recorder = hub.start_recording(tmp_path / "capture")
    parser = FrameParser(p)
    values = np.zeros((600, 2), dtype=np.float32)
    frame = make_frame(p, values)
    for item in parser.feed(frame):
        hub.accept(item)
    updated = p.clone()
    updated.rx.channel_info[0].name = "Temperature"
    hub.update_metadata(updated, transition_frames=2)
    for item in parser.feed(frame * 3):
        hub.accept(item)
    for item in parser.feed(b"bad" + frame):
        hub.accept(item)
    hub.stop_recording()
    recorder.join(5)
    assert not recorder.failed
    files = session_files(recorder.root)
    assert len(files) == 3
    reader = SegmentReader(files[1])
    assert reader.profile.rx.channel_info[0].name == "Temperature"
    valid = reader.read()[2]
    assert not valid[:1200].any()
    assert valid[1200:].all()


def test_generic_frames_are_fifo_and_do_not_change_channel_semantics():
    p = Profile()
    worker = AcquisitionWorker(p, DataHub(p))
    port = FakeSerial()
    packets = [encode_values(p, [0.5, word]) for word in (17, 80, 999)]
    for packet in packets:
        worker.send(packet)
    worker._commands(port)
    assert port.writes == packets
    assert worker.hub.profile.to_dict() == p.to_dict()
    assert worker.hub.segment == 0


def test_initial_alignment_keeps_first_segment_then_live_error_splits(tmp_path):
    p = Profile()
    hub = DataHub(p)
    recorder = hub.start_recording(tmp_path / "capture")
    parser = FrameParser(p)
    values = np.arange(1200, dtype=np.float32).reshape(600, 2)
    frame = make_frame(p, values)
    wire = frame[200:] + frame * 2
    for pos in range(0, len(wire), 617):
        for item in parser.feed(wire[pos:pos + 617]):
            hub.accept(item)
    assert hub.frames == 2 and hub.segment == 0
    assert parser.resyncs == 0
    assert hub.ring.count == 1200
    for item in parser.feed(b"bad" + frame):
        hub.accept(item)
    assert parser.resyncs > 0 and hub.segment == 1
    hub.stop_recording()
    recorder.join(5)
    assert not recorder.failed
    files = session_files(recorder.root)
    assert len(files) == 2
    assert SegmentReader(files[0]).rows == 1200
    assert SegmentReader(files[1]).rows == 600
    np.testing.assert_array_equal(SegmentReader(files[0]).read(physical=False)[1], np.tile(values, (2, 1)))
