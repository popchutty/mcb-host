from __future__ import annotations

from dataclasses import dataclass

from .config import Profile, dtype_for

DEFAULT_BUFFER_POINTS = 600_000
MAX_BUFFER_POINTS = 32_000_000
MAX_BUFFER_BYTES = 256 * 1024**2


@dataclass
class ScopeSettings:
    buffer_points: int = DEFAULT_BUFFER_POINTS

    @staticmethod
    def row_bytes(profile: Profile) -> int:
        return profile.rx.channels * dtype_for(profile.rx.data_type, profile.serial.byte_order).itemsize + 1

    @classmethod
    def point_limit(cls, profile: Profile) -> int:
        return min(MAX_BUFFER_POINTS, MAX_BUFFER_BYTES // cls.row_bytes(profile))

    def validate(self, profile: Profile) -> None:
        if (not isinstance(self.buffer_points, int) or isinstance(self.buffer_points, bool)
                or not 1 <= self.buffer_points <= MAX_BUFFER_POINTS):
            raise ValueError("缓存点数必须是 1–32,000,000 之间的整数")
        if self.buffer_points > self.point_limit(profile):
            raise ValueError("缓存占用超过 256 MiB，请减少缓存点数")

    def plan(self, profile: Profile) -> tuple[int, int, float]:
        self.validate(profile)
        return self.buffer_points, self.buffer_points * self.row_bytes(profile), self.buffer_points / profile.rx.sample_rate
