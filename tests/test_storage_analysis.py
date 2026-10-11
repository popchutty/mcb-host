import csv
import json
import time

import numpy as np
import pytest
import h5py
from scipy.io import loadmat

from sciscope.acquisition import RingBuffer
from sciscope.analysis import amplitude_spectrum, peak_trace, statistics
from sciscope.config import Profile
from sciscope.storage import FrameRecord, SegmentReader, SessionRecorder, export_segment, session_files


def fixture_session(tmp_path):
    p = Profile()
    p.rx.samples_per_frame = 600
    for c in p.rx.channel_info:
        c.scale = 1
    recorder = SessionRecorder(tmp_path / "session", p, rotate_seconds=0.1)
    recorder.start()
    arrays = []
    for i in range(9):
        data = np.arange(i * 1200, (i + 1) * 1200, dtype=np.float32).reshape(600, 2)
        arrays.append(data)
        assert recorder.submit(FrameRecord(data, time.time_ns(), i * 600, 0, i == 0, p))
    recorder.event({"type": "test"})
    recorder.stop()
    recorder.join(10)
    assert not recorder.is_alive() and not recorder.failed
    return p, recorder.root, arrays


def test_record_rotation_data_and_metadata(tmp_path):
    p, root, arrays = fixture_session(tmp_path)
    files = session_files(root)
    assert len(files) == 3
    collected = []
    for file in files:
        reader = SegmentReader(file)
        t, values, valid = reader.read()
        collected.append(values)
        assert reader.closed_cleanly
        assert reader.profile.to_dict() == p.to_dict()
        np.testing.assert_allclose(t, np.arange(len(t)) / p.rx.sample_rate)
    np.testing.assert_array_equal(np.concatenate(collected), np.concatenate(arrays))
    assert not SegmentReader(files[0]).read()[2][:600].any()
    assert SegmentReader(files[0]).read()[2][600:].all()
    assert json.loads((root / "session.json").read_text())["complete"]
    assert json.loads((root / "events.jsonl").read_text())["type"] == "test"


def test_csv_and_split_mat_roundtrip(tmp_path):
    _, root, _ = fixture_session(tmp_path)
    reader = SegmentReader(session_files(root)[0])
    t, expected, valid = reader.read()
    files = export_segment(reader, tmp_path / "output.csv", "csv")
    with files[0].open(encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    values = np.asarray([[float(v) for v in row[3:]] for row in rows[1:]])
    np.testing.assert_array_equal(values, expected)
    parts = export_segment(reader, tmp_path / "output.mat", "mat", mat_limit=65536 + 25 * 500)
    assert len(parts) > 1
    restored = [loadmat(file) for file in parts]
    np.testing.assert_array_equal(np.concatenate([x["data"] for x in restored]), expected)
    np.testing.assert_array_equal(np.concatenate([x["valid"][:, 0] for x in restored]), valid)
    np.testing.assert_allclose(np.concatenate([x["time_s"][:, 0] for x in restored]), t)


def test_analysis_uses_original_and_rejects_gaps():
    fs, n, frequency, amplitude = 20000, 20000, 1000, 3.0
    t = np.arange(n) / fs
    data = (amplitude * np.sin(2 * np.pi * frequency * t) + 2)[:, None]
    stats = statistics(data)
    assert stats["均值"][0] == pytest.approx(2)
    assert stats["RMS"][0] == pytest.approx(np.sqrt(4 + amplitude**2 / 2))
    f, spectrum = amplitude_spectrum(data, fs)
    peak = np.argmax(spectrum[:, 0])
    assert f[peak] == frequency
    assert spectrum[peak, 0] == pytest.approx(amplitude)
    valid = np.ones(n, dtype=bool)
    valid[0] = False
    with pytest.raises(ValueError, match="连续有效"):
        amplitude_spectrum(data, fs, valid)


def test_ring_wrap_is_bounded_and_keeps_original():
    p = Profile()
    p.rx.sample_rate = 100
    p.rx.samples_per_frame = 3
    ring = RingBuffer(p, seconds=0.1)
    data = np.arange(40, dtype=np.float32).reshape(20, 2)
    ring.append(data[:7])
    ring.append(data[7:12], False)
    ring.append(data[12:])
    start, recent, valid = ring.snapshot()
    assert start == 10 and ring.capacity == 10
    np.testing.assert_array_equal(recent, data[-10:])
    assert not valid[:2].any() and valid[2:].all()
    ring.clear()
    assert len(ring.snapshot()[1]) == 0


def test_peak_trace_keeps_single_sample_spikes_at_original_times():
    t = np.arange(600000) / 20000
    data = np.zeros((600000, 2))
    data[23457, 0], data[349901, 1] = 9, -11
    for channel, index, peak in [(0, 23457, 9), (1, 349901, -11)]:
        dt, shown = peak_trace(t, data[:, channel])
        assert len(dt) <= 1404
        assert np.all(np.diff(dt) >= 0)
        assert shown.max() == 9 if peak > 0 else shown.min() == -11
        assert dt[shown == peak].tolist() == [t[index]]
    assert data[23457, 0] == 9


def test_peak_trace_preserves_peak_order_endpoints_and_invalid_gaps():
    t = np.arange(2000) / 20000
    values = np.zeros(2000)
    values[12], values[35] = 10, -5
    values[1739] = 11
    values[72:78] = np.nan
    original = values.copy()
    dt, shown = peak_trace(t, values, bins=40)
    assert dt[0] == t[0] and dt[-1] == t[-1]
    assert np.all(np.diff(dt) >= 0)
    assert dt[shown == 10][0] < dt[shown == -5][0]
    assert dt[shown == 11].tolist() == [t[1739]]
    assert np.isnan(shown[(dt >= t[50]) & (dt < t[100])]).all()
    np.testing.assert_array_equal(values, original)
    _, missing = peak_trace(t, np.full(2000, np.nan), bins=40)
    assert np.isnan(missing).all()
    short_t, short_values = peak_trace(t[:30], values[:30], bins=40)
    np.testing.assert_array_equal(short_t, t[:30])
    np.testing.assert_array_equal(short_values, values[:30])


def test_ring_range_snapshot_clamps_wraps_and_does_not_alias():
    p = Profile()
    p.rx.sample_rate, p.rx.samples_per_frame = 100, 3
    ring = RingBuffer(p, seconds=0.1)
    data = np.arange(50, dtype=np.float32).reshape(25, 2)
    ring.append(data[:7])
    ring.append(data[7:18], False)
    ring.append(data[18:])
    assert ring.bounds() == (15, 25)
    start, shown, valid = ring.snapshot_range(17, 22)
    assert start == 17
    np.testing.assert_array_equal(shown, data[17:22])
    np.testing.assert_array_equal(valid, [False, True, True, True, True])
    shown[:] = -1
    np.testing.assert_array_equal(ring.snapshot_range(17, 22)[1], data[17:22])
    start, shown, _ = ring.snapshot_range(-100, 17)
    assert start == 15
    np.testing.assert_array_equal(shown, data[15:17])
    start, shown, _ = ring.snapshot_range(30, 40)
    assert start == 25 and shown.shape == (0, 2)
    start, shown, _ = ring.snapshot_range(22, 18)
    assert start == 22 and not len(shown)


def test_queue_overflow_marks_incomplete_without_affecting_monitor(tmp_path):
    p = Profile()
    recorder = SessionRecorder(tmp_path / "overflow", p)
    recorder.pending_bytes = 32 * 1024**2
    assert not recorder.submit(FrameRecord(np.zeros((600, 2), dtype=np.float32), time.time_ns(), 0, 0, False, p))
    assert recorder.failed
    recorder.start()
    recorder.join(5)
    assert not json.loads((recorder.root / "session.json").read_text(encoding="utf-8"))["complete"]


def test_disk_failure_is_reported_and_thread_terminates(tmp_path, monkeypatch):
    p = Profile()
    recorder = SessionRecorder(tmp_path / "diskfailure", p)
    def fail(*args, **kwargs):
        raise OSError("disk full injected")
    monkeypatch.setattr(h5py, "File", fail)
    recorder.start()
    recorder.submit(FrameRecord(np.zeros((600, 2), dtype=np.float32), time.time_ns(), 0, 0, False, p))
    recorder.join(5)
    assert not recorder.is_alive()
    assert "disk full injected" in recorder.failed
    assert not json.loads((recorder.root / "session.json").read_text(encoding="utf-8"))["complete"]
