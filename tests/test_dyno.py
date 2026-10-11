import struct
import json
from pathlib import Path
from dataclasses import replace

import numpy as np
import pytest

from sciscope.config import Channel, Profile
from sciscope.dyno import DynoSettings, MotorBases, dyno_mapped_profile, dyno_packet, dyno_profile
from sciscope.protocol import encode_values, physical_values


@pytest.mark.parametrize("fixed", [False, True])
@pytest.mark.parametrize("order,prefix", [("little", "<"), ("big", ">")])
@pytest.mark.parametrize("selection", range(16))
def test_dyno_command_order_control_bits_and_bytes(fixed, order, prefix, selection):
    p = dyno_profile(fixed)
    p.serial.byte_order = order
    p.tx.header, p.tx.terminator = "'S'", "'E'"
    # Business commands bypass generic field scaling.
    p.tx.fields[0].scale, p.tx.fields[0].offset = 7, 123
    s = DynoSettings(motor1=MotorBases(n_base=1024), motor2=MotorBases(i_base=2),
                     rpm=512, imag_ref=0.5, imag_pos=270, selection=selection)
    for enabled in (False, True):
        control = selection * 16 + enabled
        values = [1 << 27, control, 1 << 26, 3 << 26] if fixed else [0.5, control, 0.25, 0.75]
        expected = b"S" + struct.pack(prefix + ("IIII" if fixed else "ffff"), *values) + b"E"
        assert dyno_packet(p, s, enabled) == expected


def test_dyno_q28_negative_and_simplest_rounding():
    p = dyno_profile(True)
    s = DynoSettings(motor1=MotorBases(n_base=1), motor2=MotorBases(i_base=1),
                     rpm=-0.001, imag_ref=-0.25, imag_pos=-90, selection=15)
    raw = np.frombuffer(dyno_packet(p, s, True), dtype="<u4")
    assert raw.tolist() == [(-268435) & 0xFFFFFFFF, 241, (-2**26) & 0xFFFFFFFF, (-2**26) & 0xFFFFFFFF]
    for field, value in [("rpm", 8), ("imag_ref", -8.01), ("imag_pos", 2880)]:
        with pytest.raises(ValueError, match="Q28"):
            dyno_packet(p, replace(s, **{field: value}), False)


@pytest.mark.parametrize("fixed", [False, True])
def test_all_dyno_groups_use_the_correct_motor_and_pu_base(fixed):
    s = DynoSettings(motor1=MotorBases(1000, 2, 3, 4, 5), motor2=MotorBases(2000, 6, 7, 8, 9))
    expected = [
        [("M1_Speed_ref", "rpm", 1000), ("M2_Speed_feedback", "rpm", 2000)],
        [("M1_Speed_ref", "rpm", 1000), ("M1_Speed_feedback", "rpm", 1000)],
        [("M1_Id_ref", "A", 2), ("M1_Id_feedback", "A", 2)],
        [("M1_Iq_ref", "A", 2), ("M1_Iq_feedback", "A", 2)],
        [("M1_Vd", "V", 3), ("M1_Vq", "V", 3)],
        [("M1_Ia", "A", 2), ("M1_Ib", "A", 2)],
        [("M1_Pm", "W", 4), ("M1_Te", "N·m", 5)],
        [("M2_Id_ref", "A", 6), ("M2_Id_feedback", "A", 6)],
        [("M2_Iq_ref", "A", 6), ("M2_Iq_feedback", "A", 6)],
        [("M2_Vd", "V", 7), ("M2_Vq", "V", 7)],
        [("M2_Ia", "A", 6), ("M2_Ib", "A", 6)],
        [("M2_Pm", "W", 8), ("M2_Te", "N·m", 9)],
        [("M1_Pm", "W", 4), ("M2_Pm", "W", 8)],
        [("M1_Te", "N·m", 5), ("M2_Te", "N·m", 9)],
        [("M1_Position", "PU", 1), ("M2_Position", "PU", 1)],
        [("M1_Ia", "A", 2), ("M2_Ia", "A", 6)],
    ]
    p = dyno_profile(fixed)
    original = p.to_dict()
    for selection, channels in enumerate(expected):
        mapped = dyno_mapped_profile(p, replace(s, selection=selection))
        assert [(c.name, c.unit, c.scale) for c in mapped.rx.channel_info] == channels
        assert all(c.fractional_bits == (28 if fixed else 0) and c.signed_bits == fixed for c in mapped.rx.channel_info)
        raw = np.asarray([[2**27, (-2**26) & 0xFFFFFFFF]], dtype=np.uint32) if fixed else np.asarray([[0.5, -0.25]], dtype=np.float32)
        np.testing.assert_allclose(physical_values(raw, mapped), [[0.5 * channels[0][2], -0.25 * channels[1][2]]])
    assert p.to_dict() == original


@pytest.mark.parametrize("fixed", [False, True])
def test_dyno_profile_roundtrip_and_generic_isolation(tmp_path, fixed):
    p = dyno_profile(fixed)
    assert p.tx.data_length == 4 and p.rx.channels == 2 and p.rx.samples_per_frame == 600
    assert p.serial.baudrate == 12_000_000 and p.serial.byte_order == "little"
    assert p.rx.header == "'SSSS'" and p.rx.terminator == "'EEEE'"
    assert set(p.tools) == {"dyno"} and "enabled" not in p.tools["dyno"]
    settings = DynoSettings.from_profile(p)
    settings.motor2.p_base = 123
    settings.imag_pos, settings.selection = 270, 12
    settings.save_to(p)
    p.save(tmp_path / "dyno.json")
    restored = Profile.load(tmp_path / "dyno.json")
    assert restored.to_dict() == p.to_dict()
    assert DynoSettings.from_profile(restored) == settings
    expected = struct.pack("<IIII" if fixed else "<ffff", 1, 999, 2, 3)
    assert encode_values(p, [1, 999, 2, 3]) == expected


def test_dyno_rejects_incompatible_formats_and_invalid_settings():
    with pytest.raises(ValueError, match="四个"):
        dyno_packet(Profile(), DynoSettings(), False)
    p = dyno_profile()
    p.rx.channels = 8
    p.rx.channel_info = [Channel(f"CH{i}") for i in range(8)]
    # Commands can still be used after extending target telemetry; automatic
    # two-channel mapping must be explicitly disabled in that case.
    assert len(dyno_packet(p, DynoSettings(), False)) == 16
    with pytest.raises(ValueError, match="两通道"):
        dyno_mapped_profile(p, DynoSettings())
    for settings in [DynoSettings(selection=16), DynoSettings(rpm=float("nan")),
                     DynoSettings(motor1=MotorBases(n_base=0))]:
        with pytest.raises(ValueError):
            dyno_packet(dyno_profile(), settings, False)


def test_dyno_matches_independent_matlab_reference():
    fixtures = json.loads((Path(__file__).parent / "fixtures" / "dyno_matlab_reference.json").read_text(encoding="utf-8"))
    settings = DynoSettings(motor1=MotorBases(**fixtures["motor1"]), motor2=MotorBases(**fixtures["motor2"]))
    for case in fixtures["cases"]:
        configured = replace(settings, **{key: case[key] for key in ("rpm", "imag_ref", "imag_pos", "selection")})
        for fixed, name in ((False, "single_hex"), (True, "q28_hex")):
            assert dyno_packet(dyno_profile(fixed), configured, case["enabled"]) == bytes.fromhex(case[name])
    for group in fixtures["groups"]:
        profile = dyno_mapped_profile(dyno_profile(), replace(settings, selection=group["selection"]))
        np.testing.assert_allclose([c.scale for c in profile.rx.channel_info], group["scales"], rtol=0, atol=0)
