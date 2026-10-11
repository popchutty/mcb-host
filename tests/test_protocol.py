import copy
import struct

import numpy as np
import pytest

from sciscope.config import Channel, Profile, TYPES
from sciscope.motor import FocSettings, official_profile, motor_packet
from sciscope.protocol import FrameParser, encode_values, make_frame, marker_bytes, physical_values


@pytest.mark.parametrize("name,fmt,values", [
    ("single", "f", [0.5, -17]), ("int8", "b", [-128, 127]), ("uint8", "B", [0, 255]),
    ("int16", "h", [-32768, 32767]), ("uint16", "H", [0, 65535]),
    ("int32", "i", [-2147483648, 2147483647]), ("uint32", "I", [0, 4294967295])])
@pytest.mark.parametrize("order,prefix", [("little", "<"), ("big", ">")])
def test_all_types_exact_bytes(name, fmt, values, order, prefix):
    p = Profile()
    p.tx.data_type, p.serial.byte_order = name, order
    p.tx.header, p.tx.terminator = "'S'", "[69]"
    assert encode_values(p, values) == b"S" + struct.pack(prefix + fmt * 2, *values) + b"E"


def test_official_wire_bytes():
    p = Profile()
    expected = bytes.fromhex("00 00 00 3F 00 00 88 41")
    assert encode_values(p, [0.5, 17]) == expected
    assert motor_packet(p, FocSettings(rpm=4107 / 2), True) == expected


def test_current_profiles_and_saved_json_have_no_reference_fields(tmp_path):
    from pathlib import Path
    import json
    from sciscope.dyno import dyno_profile

    profiles = Path(__file__).resolve().parents[1] / "profiles"
    for path in profiles.glob("*.json"):
        original = json.loads(path.read_text(encoding="utf-8"))
        assert "reference" not in original["tx"]
        p = Profile.load(path)
        assert not hasattr(p.tx, "reference")
        p.save(tmp_path / path.name)
        assert json.loads((tmp_path / path.name).read_text(encoding="utf-8")) == original
    assert "reference" not in dyno_profile().to_dict()["tx"]


@pytest.mark.parametrize("value,mode,expected", [("''", "ASCII", b""), ("'SS'", "ASCII", b"SS"),
    ("[83 83]", "ASCII", b"SS"), ("[0,255]", "ASCII", b"\0\xff"), ("83", "ASCII", b"S"),
    ("0x53 53", "HEX", b"SS"), ("53,53", "HEX", b"SS")])
def test_marker_inputs(value, mode, expected):
    assert marker_bytes(value, mode) == expected


@pytest.mark.parametrize("value", ["[256]", "[system('bad')]", "中文", "'unclosed", "[1 -1]"])
def test_invalid_markers(value):
    with pytest.raises((ValueError, UnicodeError)):
        marker_bytes(value)


@pytest.mark.parametrize("name", ["int8", "uint8", "int16", "uint16", "int32", "uint32"])
def test_integer_overflow_not_silently_truncated(name):
    p = Profile()
    p.tx.data_type = name
    limits = np.iinfo(np.dtype(TYPES[name]))
    for bad in [int(limits.max) + 1, int(limits.min) - 1, 0.5]:
        with pytest.raises(ValueError):
            encode_values(p, [bad, 0])
    with pytest.raises(ValueError):
        encode_values(p, [0])


def test_scale_and_q28_negative():
    p = official_profile(True)
    payload = motor_packet(p, FocSettings(rpm=-4107 / 2, selection=4), True)
    assert payload == struct.pack("<II", (-2**27) & 0xFFFFFFFF, 65)
    raw = np.asarray([[(-2**27) & 0xFFFFFFFF, 2**27]], dtype=np.uint32)
    np.testing.assert_allclose(physical_values(raw, p, False), [[-0.5, 0.5]])
    p = Profile()
    p.tx.fields[0].scale = 2
    p.tx.fields[0].offset = 1
    assert encode_values(p, [2, 17]) == struct.pack("<ff", 0.5, 17)


@pytest.mark.parametrize("order", ["<", ">"])
@pytest.mark.parametrize("physical", [False, True])
def test_float_decode_ignores_integer_conversion_settings(order, physical):
    p = Profile()
    p.rx.channel_info = [Channel("Speed", "RPM", 4107, 10, 28, True), Channel("Current", "A", 2, -1, 8, True)]
    raw = np.asarray([[-0.5, 0.25], [0.5, -0.25]], dtype=order + "f4")
    expected = raw.astype(np.float64)
    if physical:
        expected = expected * [4107, 2] + [10, -1]
    np.testing.assert_allclose(physical_values(raw, p, physical), expected)


@pytest.mark.parametrize("data_type", ["int8", "uint8", "int16", "uint16", "int32", "uint32"])
def test_integer_fixed_point_decode_keeps_fraction_and_sign(data_type):
    p = Profile()
    p.rx.data_type = data_type
    p.rx.channel_info = [Channel("Negative", fractional_bits=2, signed_bits=True), Channel("Positive", fractional_bits=2)]
    dtype = np.dtype(TYPES[data_type])
    negative = 2 ** (dtype.itemsize * 8) - 2 if dtype.kind == "u" else -2
    raw = np.asarray([[negative, 2]], dtype=dtype)
    np.testing.assert_allclose(physical_values(raw, p, False), [[-0.5, 0.5]])


@pytest.mark.parametrize("layout", ["interleaved", "planar"])
def test_fragmentation_and_marker_inside_payload(layout):
    p = Profile()
    p.rx.samples_per_frame = 12
    p.rx.data_type = "uint8"
    p.rx.layout = layout
    data = np.arange(24, dtype=np.uint8).reshape(12, 2)
    data[0:2] = np.frombuffer(b"SSSS", dtype=np.uint8).reshape(2, 2)
    data[5:7] = np.frombuffer(b"EEEE", dtype=np.uint8).reshape(2, 2)
    frame = make_frame(p, data)
    wire = frame * 3
    parser = FrameParser(p)
    rng = np.random.default_rng(4)
    result = []
    pos = 0
    while pos < len(wire):
        n = int(rng.integers(1, 23))
        result.extend(parser.feed(wire[pos:pos + n]))
        pos += n
    assert len(result) == 3
    for parsed in result:
        np.testing.assert_array_equal(parsed.data, data)
    assert not parser.buffer


def test_dropped_byte_and_bad_tail_resync():
    p = Profile()
    p.rx.samples_per_frame = 4
    data = np.arange(8, dtype=np.float32).reshape(4, 2)
    frame = make_frame(p, data)
    bad = frame[:10] + frame[11:]
    parser = FrameParser(p)
    first = parser.feed(frame)
    assert len(first) == 1 and parser.resyncs == 0
    result = parser.feed(b"noise" + bad + frame[:-1] + b"X" + frame * 2)
    assert len(result) == 2
    assert parser.resyncs and parser.discarded
    assert result[0].generation > 0
    np.testing.assert_array_equal(result[1].data, data)


@pytest.mark.parametrize("offset", [1, 3, 4, 52, 2404, 4805, 4807])
def test_connecting_mid_frame_is_initial_alignment_not_an_error(offset):
    p = Profile()
    data = np.arange(1200, dtype=np.float32).reshape(600, 2)
    frame = make_frame(p, data)
    wire = frame[offset:] + frame * 2
    parser = FrameParser(p)
    result = []
    rng = np.random.default_rng(offset)
    pos = 0
    while pos < len(wire):
        width = int(rng.integers(1, 1000))
        result.extend(parser.feed(wire[pos:pos + width]))
        pos += width
    assert len(result) == 2
    assert parser.discarded == len(frame) - offset
    assert parser.resyncs == parser.generation == 0
    assert all(item.generation == 0 for item in result)
    for item in result:
        np.testing.assert_array_equal(item.data, data)


def test_initial_header_candidate_with_wrong_tail_does_not_count():
    p = Profile()
    p.rx.samples_per_frame = 4
    data = np.arange(8, dtype=np.float32).reshape(4, 2)
    frame = make_frame(p, data)
    parser = FrameParser(p)
    # A header alone is not enough to establish synchronization.
    assert not parser.feed(frame[:-1] + b"X")
    assert parser.frames == parser.resyncs == parser.generation == 0
    result = parser.feed(frame * 2)
    assert len(result) == 2 and parser.discarded > 0
    assert parser.resyncs == parser.generation == 0
    np.testing.assert_array_equal(result[0].data, data)


def test_live_loss_after_initial_alignment_still_counts_in_same_chunk():
    p = Profile()
    p.rx.samples_per_frame = 4
    data = np.arange(8, dtype=np.float32).reshape(4, 2)
    frame = make_frame(p, data)
    parser = FrameParser(p)
    result = parser.feed(frame[7:] + frame + b"bad" + frame)
    assert len(result) == 2
    assert result[0].generation == 0
    assert result[1].generation > 0
    assert parser.resyncs > 0
    for item in result:
        np.testing.assert_array_equal(item.data, data)


def test_initial_alignment_with_only_a_terminator():
    p = Profile()
    p.rx.header = "''"
    p.rx.samples_per_frame = 4
    data = np.arange(8, dtype=np.float32).reshape(4, 2)
    frame = make_frame(p, data)
    parser = FrameParser(p)
    result = parser.feed(frame[3:] + frame * 2)
    assert len(result) == 2 and parser.discarded == len(frame) - 3
    assert parser.resyncs == parser.generation == 0
    np.testing.assert_array_equal(result[0].data, data)


@pytest.mark.parametrize("configured,actual", [(2, 8), (8, 2)])
@pytest.mark.parametrize("chunk_size", [1, 617, 65536])
def test_initial_channel_count_mismatch_counts_once(configured, actual, chunk_size):
    p = Profile()
    p.rx.channels = configured
    target = p.clone()
    target.rx.channels = actual
    values = np.arange(600 * actual, dtype=np.float32).reshape(600, actual)
    frame = make_frame(target, values)
    # Enough complete adjacent frames for both the length probe and the
    # configured parser, with the first read starting inside a payload.
    wire = frame[52:] + frame * 10
    parser = FrameParser(p)
    for pos in range(0, len(wire), chunk_size):
        assert not parser.feed(wire[pos:pos + chunk_size])
    assert parser.frames == parser.generation == 0
    assert parser.resyncs == 1
    assert f"配置 {parser.frame_size:,} B" in parser.protocol_warning
    assert f"检测约 {len(frame):,} B" in parser.protocol_warning
    assert "通道数" in parser.protocol_warning
    assert len(parser.buffer) < parser.frame_size
    assert len(parser._startup_suffix) < len(parser._boundary)
    # Continued mismatch does not flood the count, warning, or buffer.
    warning = parser.protocol_warning
    for _ in range(30):
        assert not parser.feed(frame)
    assert parser.resyncs == 1 and parser.protocol_warning == warning
    assert len(parser.buffer) < parser.frame_size
    # If a correctly sized stream appears later, accept it without creating
    # an empty recording segment for the startup error.
    correct = make_frame(p, np.zeros((600, configured), dtype=np.float32))
    received = parser.feed(correct * 2)
    assert received and all(item.generation == 0 for item in received)
    assert parser.resyncs == 1 and not parser.protocol_warning


def test_initial_samples_per_frame_mismatch_is_also_detected():
    p = Profile()
    target = p.clone()
    target.rx.samples_per_frame = 300
    frame = make_frame(target, np.zeros((300, 2), dtype=np.float32))
    parser = FrameParser(p)
    assert not parser.feed(frame * 10)
    assert parser.resyncs == 1
    assert f"检测约 {len(frame):,} B" in parser.protocol_warning


@pytest.mark.parametrize("offset", [1, 7, 9, 100, 135])
def test_initial_repeated_boundary_markers_in_payload_do_not_count(offset):
    p = Profile()
    p.rx.data_type = "uint8"
    p.rx.samples_per_frame = 64
    data = np.frombuffer(b"EEEESSSS" * 16, dtype=np.uint8).reshape(64, 2)
    frame = make_frame(p, data)
    parser = FrameParser(p)
    wire = frame[offset:] + frame * 3
    for pos in range(0, len(wire), 3):
        parser.feed(wire[pos:pos + 3])
    assert parser.frames >= 2
    assert parser.resyncs == parser.generation == 0
    assert not parser.protocol_warning


@pytest.mark.parametrize("header,tail", [("''", "'EEEE'"), ("'SSSS'", "''"), ("''", "''")])
def test_startup_length_probe_requires_both_markers(header, tail):
    p = Profile()
    p.rx.header, p.rx.terminator = header, tail
    parser = FrameParser(p)
    parser.feed(b"EEEESSSS" * (parser.frame_size // 2))
    assert not parser.protocol_warning


def test_profile_roundtrip_and_bandwidth(tmp_path):
    p = Profile()
    p.save(tmp_path / "p.json")
    assert Profile.load(tmp_path / "p.json").to_dict() == p.to_dict()
    p.rx.channels = 14
    p.rx.channel_info = [Channel(str(i)) for i in range(14)]
    p.validate()
    assert p.bandwidth()[1] == pytest.approx(0.9335555555555556)
    p.rx.channels = 15
    p.rx.channel_info.append(Channel("15"))
    with pytest.raises(ValueError, match="理论容量"):
        p.validate()
    invalid = copy.deepcopy(p.to_dict())
    invalid["version"] = 99
    with pytest.raises(ValueError):
        Profile.from_dict(invalid)


def test_protocol_json_excludes_scope_settings_and_reads_135_files(tmp_path):
    p = Profile()
    packet = encode_values(p, [0.5, 17])
    previous = p.to_dict()
    previous.update(version=3, display={"buffer_points": 1_200_000})
    restored = Profile.from_dict(previous)
    restored.save(tmp_path / "protocol.json")
    assert restored.version == 2
    assert "display" not in restored.to_dict()
    assert Profile.load(tmp_path / "protocol.json").to_dict() == p.to_dict()
    assert encode_values(restored, [0.5, 17]) == packet


def test_legacy_config_migrates_without_automatic_business_mapping():
    p = Profile()
    p.rx.channel_info[0] = Channel("自定义温度", "℃", 0.25, 10)
    legacy = p.to_dict()
    legacy.update(version=1, motor_preset=True, motor_n_base=3000,
                  motor_i_base=20, debug_selection=4)
    legacy.pop("tools")
    restored = Profile.from_dict(legacy)
    assert restored.version == 2
    assert restored.rx.channel_info == p.rx.channel_info
    assert restored.tools["foc"]["n_base"] == 3000
    assert "motor_preset" not in restored.to_dict()
    # Arbitrary control values remain ordinary payload elements.
    assert encode_values(restored, [0.5, 999]) == struct.pack("<ff", 0.5, 999)
