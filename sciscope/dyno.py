"""Optional adapter for mcb_pmsm_foc_f28379d_dyno and its official host."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import math

import numpy as np

from .config import Channel, Profile
from .protocol import encode_values


# Selection numbers are the actual zero-based values of the target switch.
# Each signal identifies its motor, name, PU base, and display unit.
DYNO_DEBUG_PAIRS = [
    ((1, "Speed_ref", "n_base", "rpm"), (2, "Speed_feedback", "n_base", "rpm")),
    ((1, "Speed_ref", "n_base", "rpm"), (1, "Speed_feedback", "n_base", "rpm")),
    ((1, "Id_ref", "i_base", "A"), (1, "Id_feedback", "i_base", "A")),
    ((1, "Iq_ref", "i_base", "A"), (1, "Iq_feedback", "i_base", "A")),
    ((1, "Vd", "v_base", "V"), (1, "Vq", "v_base", "V")),
    ((1, "Ia", "i_base", "A"), (1, "Ib", "i_base", "A")),
    ((1, "Pm", "p_base", "W"), (1, "Te", "t_base", "N·m")),
    ((2, "Id_ref", "i_base", "A"), (2, "Id_feedback", "i_base", "A")),
    ((2, "Iq_ref", "i_base", "A"), (2, "Iq_feedback", "i_base", "A")),
    ((2, "Vd", "v_base", "V"), (2, "Vq", "v_base", "V")),
    ((2, "Ia", "i_base", "A"), (2, "Ib", "i_base", "A")),
    ((2, "Pm", "p_base", "W"), (2, "Te", "t_base", "N·m")),
    ((1, "Pm", "p_base", "W"), (2, "Pm", "p_base", "W")),
    ((1, "Te", "t_base", "N·m"), (2, "Te", "t_base", "N·m")),
    ((1, "Position", None, "PU"), (2, "Position", None, "PU")),
    ((1, "Ia", "i_base", "A"), (2, "Ia", "i_base", "A")),
]


@dataclass
class MotorBases:
    # Computed from the supplied Teknic2310P / BoostXL-DRV8305 data script.
    n_base: float = 4107.0
    i_base: float = 21.428571428571427
    v_base: float = 13.856406460551019
    p_base: float = 445.38449337485412
    t_base: float = 0.82226766688621544

    def validate(self):
        if not all(math.isfinite(value) and value > 0 for value in asdict(self).values()):
            raise ValueError("对拖电机基准值必须为正有限数值")


@dataclass
class DynoSettings:
    motor1: MotorBases = field(default_factory=MotorBases)
    motor2: MotorBases = field(default_factory=MotorBases)
    rpm: float = 1200.0
    imag_ref: float = 1.0
    imag_pos: float = 90.0
    selection: int = 0
    map_channels: bool = False

    def validate(self):
        self.motor1.validate()
        self.motor2.validate()
        if (not all(math.isfinite(value) for value in (self.rpm, self.imag_ref, self.imag_pos))
                or not isinstance(self.selection, int) or self.selection not in range(16)):
            raise ValueError("对拖给定值或调试组无效")

    def save_to(self, profile: Profile):
        self.validate()
        profile.tools["dyno"] = asdict(self)

    @classmethod
    def from_profile(cls, profile: Profile):
        values = dict(profile.tools.get("dyno", {}))
        for motor in ("motor1", "motor2"):
            if motor in values:
                values[motor] = MotorBases(**values[motor])
        result = cls(**values)
        result.validate()
        return result


def dyno_packet(profile: Profile, settings: DynoSettings, enabled: bool) -> bytes:
    settings.validate()
    if profile.tx.data_length != 4 or profile.tx.data_type not in ("single", "uint32"):
        raise ValueError("对拖 FOC 控制需要四个 single 元素，或四个 uint32 元素（Q28）")
    # Match the host's single Constant -> single Gain -> conversion chain.
    with np.errstate(over="ignore", invalid="ignore"):
        speed = np.float32(settings.rpm) * np.float32(1 / settings.motor1.n_base)
        current = np.float32(settings.imag_ref) * np.float32(1 / settings.motor2.i_base)
        position = np.float32(settings.imag_pos) * np.float32(1 / 360)
    control = (settings.selection << 4) | int(enabled)
    values = [speed, control, current, position]
    if profile.tx.data_type == "uint32":
        for index in (0, 2, 3):
            value = float(values[index])
            if not math.isfinite(value) or not -8 <= value < 8:
                raise ValueError("对拖给定值超出有符号 Q28 表示范围 [-8, 8)")
            # Simplest uses round toward zero for floating-to-fixed casts.
            values[index] = math.trunc(value * (1 << 28)) & 0xFFFFFFFF
    return encode_values(profile, values, apply_scale=False)


def dyno_mapped_profile(profile: Profile, settings: DynoSettings) -> Profile:
    settings.validate()
    if profile.rx.channels != 2 or profile.rx.data_type not in ("single", "uint32"):
        raise ValueError("对拖通道映射仅支持两通道 single / uint32 上报")
    p = profile.clone()
    fixed = p.rx.data_type == "uint32"
    p.rx.channel_info = []
    for motor, signal, base, unit in DYNO_DEBUG_PAIRS[settings.selection]:
        bases = settings.motor1 if motor == 1 else settings.motor2
        scale = getattr(bases, base) if base else 1.0
        p.rx.channel_info.append(Channel(f"M{motor}_{signal}", unit, scale,
                                         fractional_bits=28 if fixed else 0, signed_bits=fixed))
    settings.save_to(p)
    return p


def dyno_profile(fixed=False) -> Profile:
    p = Profile(name="官方对拖 FOC · " + ("Q28" if fixed else "single"))
    p.tx.data_length = 4
    if fixed:
        p.tx.data_type = p.rx.data_type = "uint32"
    p.tx.fields = [Channel("M1_Speed_PU"), Channel("Control"), Channel("M2_Imag_PU"), Channel("M2_Imag_Pos_PU")]
    return dyno_mapped_profile(p, DynoSettings(map_channels=True))
