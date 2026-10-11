from __future__ import annotations

from dataclasses import asdict, dataclass, field
import copy
import json
import math
from pathlib import Path

import numpy as np

TYPES = {"single": "f4", "int8": "i1", "uint8": "u1", "int16": "i2",
         "uint16": "u2", "int32": "i4", "uint32": "u4"}


def dtype_for(name: str, order: str) -> np.dtype:
    if name not in TYPES or order not in ("little", "big"):
        raise ValueError("不支持的数据类型或字节序")
    return np.dtype(("<" if order == "little" else ">") + TYPES[name])


@dataclass
class Channel:
    name: str = "Channel"
    unit: str = ""
    scale: float = 1.0
    offset: float = 0.0
    fractional_bits: int = 0
    signed_bits: bool = False


@dataclass
class SerialSettings:
    port: str = ""
    baudrate: int = 12_000_000
    bytesize: int = 8
    parity: str = "N"
    stopbits: float = 1.0
    byte_order: str = "little"


@dataclass
class TxSettings:
    sci_module: str = "A"
    header: str = "''"
    header_mode: str = "ASCII"
    terminator: str = "''"
    terminator_mode: str = "ASCII"
    data_type: str = "single"
    length_option: str = "Length via dialog"
    data_length: int = 2
    fields: list[Channel] = field(default_factory=lambda: [Channel("Data1"), Channel("Data2")])
    send_mode: str = "manual"
    interval_ms: int = 100


@dataclass
class RxSettings:
    header: str = "'SSSS'"
    header_mode: str = "ASCII"
    terminator: str = "'EEEE'"
    terminator_mode: str = "ASCII"
    data_type: str = "single"
    channels: int = 2
    samples_per_frame: int = 600
    sample_rate: float = 20_000.0
    layout: str = "interleaved"
    channel_info: list[Channel] = field(default_factory=lambda: [Channel("CH1"), Channel("CH2")])


@dataclass
class Profile:
    version: int = 2
    name: str = "SCI · single / 2 通道"
    serial: SerialSettings = field(default_factory=SerialSettings)
    tx: TxSettings = field(default_factory=TxSettings)
    rx: RxSettings = field(default_factory=RxSettings)
    tools: dict = field(default_factory=dict)

    def clone(self) -> Profile:
        return copy.deepcopy(self)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Profile:
        data = copy.deepcopy(data)
        if data.get("version") not in (1, 2, 3):
            raise ValueError("不支持的配置版本")
        # Preserve legacy calibration and optional tool settings without enabling
        # model-specific behaviour in generic transmit/receive operations.
        if data["version"] == 1:
            if data.get("motor_preset", False):
                data["tools"] = {"foc": {"n_base": data.get("motor_n_base", 4107),
                                         "i_base": data.get("motor_i_base", 21.428571428571427),
                                         "selection": data.get("debug_selection", 1)}}
            for key in ("motor_n_base", "motor_i_base", "debug_selection", "motor_preset"):
                data.pop(key, None)
            data["version"] = 2
        # Read 1.3.5 files, but discard its misplaced plotting setting.
        data["version"] = 2
        data.pop("display", None)
        serial_data = SerialSettings(**data.pop("serial"))
        tx_data, rx_data = data.pop("tx"), data.pop("rx")
        tx_data["fields"] = [Channel(**c) for c in tx_data["fields"]]
        rx_data["channel_info"] = [Channel(**c) for c in rx_data["channel_info"]]
        profile = cls(serial=serial_data, tx=TxSettings(**tx_data), rx=RxSettings(**rx_data), **data)
        profile.validate()
        return profile

    def save(self, path: str | Path) -> None:
        self.validate()
        Path(path).write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> Profile:
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def validate(self) -> None:
        from .protocol import marker_bytes
        s, tx, rx = self.serial, self.tx, self.rx
        if self.version != 2 or s.bytesize not in (5, 6, 7, 8) or s.parity not in ("N", "E", "O", "M", "S") or s.stopbits not in (1, 1.5, 2):
            raise ValueError("串口或配置版本无效")
        if not isinstance(s.baudrate, int) or s.baudrate <= 0:
            raise ValueError("波特率必须为正整数")
        if tx.sci_module not in ("A", "B", "C", "D") or tx.length_option != "Length via dialog":
            raise ValueError("第一版仅支持 Length via dialog")
        if tx.send_mode not in ("manual", "periodic") or tx.interval_ms < 10:
            raise ValueError("周期发送间隔至少为 10 ms")
        for count in (tx.data_length, rx.channels, rx.samples_per_frame):
            if not isinstance(count, int) or count < 1:
                raise ValueError("元素数、通道数、每帧组数必须为正整数")
        if tx.data_length > 4096 or rx.channels > 256 or len(tx.fields) != tx.data_length or len(rx.channel_info) != rx.channels:
            raise ValueError("数量与字段配置不一致，或超过配置上限")
        if not math.isfinite(rx.sample_rate) or rx.sample_rate <= 0 or rx.layout not in ("interleaved", "planar"):
            raise ValueError("采样率或排列方式无效")
        for name in (tx.data_type, rx.data_type):
            dtype_for(name, s.byte_order)
        for text, mode in ((tx.header, tx.header_mode), (tx.terminator, tx.terminator_mode), (rx.header, rx.header_mode), (rx.terminator, rx.terminator_mode)):
            if len(marker_bytes(text, mode)) > 256:
                raise ValueError("帧头或帧尾不能超过 256 字节")
        if rx.channels * rx.samples_per_frame * dtype_for(rx.data_type, s.byte_order).itemsize > 16 * 1024**2:
            raise ValueError("单帧载荷不能超过 16 MiB")
        for channel in tx.fields + rx.channel_info:
            if not math.isfinite(channel.scale) or not math.isfinite(channel.offset) or channel.fractional_bits not in range(32):
                raise ValueError("通道换算参数无效")
        if any(c.scale == 0 for c in tx.fields):
            raise ValueError("下发换算比例不能为零")
        if not isinstance(self.tools, dict):
            raise ValueError("工具配置必须为对象")
        if self.bandwidth()[1] > 1:
            raise ValueError("上报字节率超过当前串口理论容量，请减少通道/采样率或提高波特率")

    def bandwidth(self) -> tuple[float, float]:
        from .protocol import marker_bytes
        r, s = self.rx, self.serial
        size = r.channels * r.samples_per_frame * dtype_for(r.data_type, s.byte_order).itemsize
        size += len(marker_bytes(r.header, r.header_mode)) + len(marker_bytes(r.terminator, r.terminator_mode))
        rate = size * r.sample_rate / r.samples_per_frame
        bits = 1 + s.bytesize + (s.parity != "N") + s.stopbits
        return rate, rate * bits / s.baudrate
