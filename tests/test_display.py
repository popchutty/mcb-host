import numpy as np
import pytest

from sciscope.acquisition import DataHub
from sciscope.config import Profile
from sciscope.display import ScopeSettings
from sciscope.protocol import FrameParser, make_frame
from sciscope.storage import SegmentReader, session_files


def test_storage_depth_is_independent_of_protocol_and_frame_length():
    p = Profile()
    before = p.to_dict()
    assert ScopeSettings(1_200_000).plan(p) == (1_200_000, 10_800_000, 60)
    assert ScopeSettings(1).plan(p) == (1, 9, 0.00005)
    assert p.to_dict() == before
    hub = DataHub(p, buffer_points=50)
    source = np.arange(1200, dtype=np.float32).reshape(600, 2)
    hub.accept(FrameParser(p).feed(make_frame(p, source))[0])
    assert hub.ring.bounds() == (550, 600)
    np.testing.assert_array_equal(hub.ring.snapshot()[1], source[-50:])


@pytest.mark.parametrize("points", [0, -1, True, 1.5, 32_000_001, 30_000_000])
def test_invalid_or_excessive_storage_depth_is_rejected(points):
    with pytest.raises(ValueError, match="缓存"):
        ScopeSettings(points).validate(Profile())


@pytest.mark.parametrize("initial,depths", [(10, [3, 14, 5, 1, 12]), (30, [40, 9, 25]), (1, [7, 2, 20])])
def test_resize_retains_latest_rows_validity_and_absolute_indices(initial, depths):
    hub = DataHub(Profile(), buffer_points=initial)
    ring = hub.ring
    source = np.arange(100, dtype=np.float32).reshape(50, 2)
    all_valid = np.ones(50, dtype=bool)
    all_valid[7:17] = False
    ring.append(source[:7])
    ring.append(source[7:17], False)
    total, count = 17, min(initial, 17)
    for depth in depths:
        hub.resize_buffer(depth)
        count = min(count, depth)
        assert hub.ring is ring and ring.bounds() == (total - count, total)
        start, data, valid = ring.snapshot()
        np.testing.assert_array_equal(data, source[start:total])
        np.testing.assert_array_equal(valid, all_valid[start:total])
        ring.append(source[total:total + 6])
        total += 6
        count = min(depth, count + 6)
        start, data, valid = ring.snapshot()
        assert start == total - count
        np.testing.assert_array_equal(data, source[start:total])
        np.testing.assert_array_equal(valid, all_valid[start:total])
    assert hub.segment == hub.generation == hub.resyncs == 0


def test_resize_does_not_split_or_truncate_recording(tmp_path):
    p = Profile()
    p.rx.samples_per_frame = 100
    hub = DataHub(p, buffer_points=150)
    recorder = hub.start_recording(tmp_path / "depth")
    parser = FrameParser(p)
    source = np.arange(2000, dtype=np.float32).reshape(1000, 2)
    try:
        for index, begin in enumerate(range(0, len(source), 100)):
            hub.resize_buffer([50, 500, 120, 1, 200][index % 5])
            hub.accept(parser.feed(make_frame(p, source[begin:begin + 100]))[0])
    finally:
        hub.stop_recording()
        recorder.join(5)
    files = session_files(recorder.root)
    assert len(files) == 1 and hub.segment == 0
    assert not recorder.failed and recorder.written_rows == len(source)
    np.testing.assert_array_equal(SegmentReader(files[0]).read(physical=False)[1], source)
