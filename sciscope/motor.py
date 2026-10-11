"""Optional FOC business adapter. The generic SCI protocol does not import it."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import math

from .config import Channel, Profile
from .protocol import encode_values

DEBUG_PAIRS = [("Speed_ref", "Speed_feedback", "rpm", "rpm"),
               ("Id_ref", "Id_feedback", "A", "A"),
               ("Iq_ref", "Iq_feedback", "A", "A"),
               ("Ia", "Ib", "A", "A"), ("Ia", "Position", "A", "rad")]


@dataclass
class FocSettings:
    n_base: float = 4107.0
    i_base: float = 21.428571428571427
    rpm: float = 2000.0
    selection: int = 1
    map_channels: bool = False

    def validate(self):
        if not all(math.isfinite(x) for x in (self.n_base, self.i_base, self.rpm)) or min(self.n_base, self.i_base) <= 0 or self.selection not in range(1, 6):
            raise ValueError("电机基准值或调试组无效")

    def save_to(self, profile):
        self.validate()
        profile.tools["foc"] = asdict(self)

    @classmethod
    def from_profile(cls, profile):
        result = cls(**profile.tools.get("foc", {}))
        result.validate()
        return result


def motor_packet(profile: Profile, settings: FocSettings, enabled: bool) -> bytes:
    settings.validate()
    if profile.tx.data_length != 2 or profile.tx.data_type not in ("single", "uint32"):
        raise ValueError("FOC 控制需要两个 single 元素，或两个 uint32 元素（Q28）")
    control = (settings.selection << 4) | int(enabled)
    speed = settings.rpm / settings.n_base
    if profile.tx.data_type == "uint32":
        raw = round(speed * (1 << 28))
        if not -(1 << 31) <= raw < (1 << 31):
            raise ValueError("转速超出 Q28 表示范围")
        return encode_values(profile, [raw & 0xFFFFFFFF, control], apply_scale=False)
    return encode_values(profile, [speed, float(control)], apply_scale=False)


def mapped_profile(profile: Profile, settings: FocSettings) -> Profile:
    settings.validate()
    if profile.rx.channels != 2 or profile.rx.data_type not in ("single", "uint32"):
        raise ValueError("FOC 通道映射仅支持两通道 single / uint32 上报")
    p = profile.clone()
    a, b, ua, ub = DEBUG_PAIRS[settings.selection - 1]
    scales = [settings.n_base] * 2 if settings.selection == 1 else [settings.i_base] * 2
    if settings.selection == 5:
        scales[1] = 2 * math.pi
    fixed = p.rx.data_type == "uint32"
    p.rx.channel_info = [Channel(name, unit, scale, fractional_bits=28 if fixed else 0, signed_bits=fixed)
                         for name, unit, scale in zip((a, b), (ua, ub), scales)]
    settings.save_to(p)
    return p


def official_profile(fixed=False):
    p = Profile(name="官方单电机 FOC · " + ("Q28" if fixed else "single"))
    if fixed:
        p.tx.data_type = p.rx.data_type = "uint32"
    p.tx.fields = [Channel("Speed_PU"), Channel("Control")]
    return mapped_profile(p, FocSettings(map_channels=True))
