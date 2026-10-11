from __future__ import annotations

from dataclasses import dataclass
import math
import re

import numpy as np

from .config import Profile, dtype_for


def marker_bytes(text: str, mode: str = "ASCII") -> bytes:
    text = text.strip()
    if mode == "HEX":
        cleaned = re.sub(r"0[xX]", "", text)
        return bytes.fromhex(cleaned.replace(",", " "))
    if mode != "ASCII":
        raise ValueError("标记输入模式必须为 ASCII 或 HEX")
    if not text or text in ("''", '""', "[]"):
        return b""
    if text.startswith("[") and text.endswith("]"):
        values = re.split(r"[\s,;]+", text[1:-1].strip())
        if any(not re.fullmatch(r"\d{1,3}", v) for v in values):
            raise ValueError("字节数组只允许 0–255 的十进制整数")
        return bytes(int(v) for v in values)
    if text.isdigit():
        return bytes([int(text)])
    if text.startswith(("'", '"')):
        if len(text) < 2 or text[-1] != text[0]:
            raise ValueError("ASCII 标记引号不匹配")
        text = text[1:-1]
    try:
        return text.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("帧头帧尾只允许 ASCII 字符；其他字节请使用 HEX") from exc


def encode_values(profile: Profile, values: list, *, apply_scale: bool = True) -> bytes:
    tx = profile.tx
    if len(values) != tx.data_length:
        raise ValueError(f"需要 {tx.data_length} 个数据元素")
    dt = dtype_for(tx.data_type, profile.serial.byte_order)
    prepared = []
    for i, value in enumerate(values):
        if isinstance(value, str):
            value = float(value) if dt.kind == "f" or apply_scale else int(value, 0)
        if apply_scale:
            c = tx.fields[i]
            value = (value - c.offset) / c.scale
        if not math.isfinite(float(value)):
            raise ValueError("发送值必须为有限数值")
        if dt.kind in "iu":
            limits = np.iinfo(dt)
            if value != int(value) or not limits.min <= value <= limits.max:
                raise ValueError(f"第 {i + 1} 个值超出 {tx.data_type} 范围或不是整数")
            value = int(value)
        elif abs(value) > np.finfo(dt).max:
            raise ValueError("浮点值超出 single 范围")
        prepared.append(value)
    return marker_bytes(tx.header, tx.header_mode) + np.asarray(prepared, dtype=dt).tobytes() + marker_bytes(tx.terminator, tx.terminator_mode)


def physical_values(data: np.ndarray, profile: Profile, physical: bool = True) -> np.ndarray:
    # Contiguous channel columns avoid repeatedly scanning interleaved rows.
    result = data.astype(np.float64, order="F")
    for i, c in enumerate(profile.rx.channel_info):
        if c.signed_bits and data.dtype.kind == "u":
            bits = data.dtype.itemsize * 8
            result[:, i] = np.where(result[:, i] >= 2 ** (bits - 1), result[:, i] - 2**bits, result[:, i])
        if c.fractional_bits and data.dtype.kind in "iu":
            result[:, i] /= 2**c.fractional_bits
        if physical:
            if c.scale != 1:
                result[:, i] *= c.scale
            if c.offset:
                result[:, i] += c.offset
    return result


@dataclass
class ParsedFrame:
    data: np.ndarray
    generation: int


class FrameParser:
    def __init__(self, profile: Profile):
        self.profile = profile
        r = profile.rx
        self.header = marker_bytes(r.header, r.header_mode)
        self.tail = marker_bytes(r.terminator, r.terminator_mode)
        self.dtype = dtype_for(r.data_type, profile.serial.byte_order)
        self.payload_size = r.channels * r.samples_per_frame * self.dtype.itemsize
        self.frame_size = len(self.header) + self.payload_size + len(self.tail)
        self.buffer = bytearray()
        self.frames = self.resyncs = self.discarded = self.generation = 0
        self.protocol_warning = ""
        self._boundary = self.tail + self.header if self.tail and self.header else b""
        self._reset_startup_probe()

    def _reset_startup_probe(self) -> None:
        self._startup_received = 0
        self._startup_suffix = b""
        self._last_boundary = None
        self._boundary_spacing = None
        self._equal_intervals = 0
        self._candidate_frame_size = None
        self._startup_length_reported = False

    def _observe_startup(self, chunk: bytes) -> None:
        if self.frames or not self._boundary or not chunk:
            return
        # A tail followed by a header locates adjacent frames. Keep only the
        # suffix needed to find a boundary split across serial reads.
        probe = self._startup_suffix + chunk
        base = self._startup_received - len(self._startup_suffix)
        self._startup_received += len(chunk)
        pos = probe.find(self._boundary)
        while pos >= 0:
            absolute = base + pos
            if self._last_boundary is not None:
                spacing = absolute - self._last_boundary
                self._equal_intervals = self._equal_intervals + 1 if spacing == self._boundary_spacing else 1
                self._boundary_spacing = spacing
                if self._equal_intervals >= 2:
                    self._candidate_frame_size = spacing
            self._last_boundary = absolute
            pos = probe.find(self._boundary, pos + 1)
        self._startup_suffix = bytes(probe[-(len(self._boundary) - 1):])

    def _check_startup_length(self) -> None:
        if self.frames:
            self.protocol_warning = ""
            self._startup_suffix = b""
            return
        candidate = self._candidate_frame_size
        # Try the configured fixed-length parser first, including enough data
        # for a full frame after mid-frame connection. Payload markers alone
        # must not turn normal initial alignment into an error.
        if (not self._startup_length_reported and self._startup_received >= 2 * self.frame_size
                and candidate is not None and candidate > len(self._boundary) and candidate != self.frame_size):
            self.resyncs += 1
            self._startup_length_reported = True
            self.protocol_warning = (f"疑似帧长不匹配（配置 {self.frame_size:,} B，检测约 {candidate:,} B），"
                                     "请检查通道数、每帧组数和数据类型。")

    def reset(self) -> None:
        self.discarded += len(self.buffer)
        self.buffer.clear()
        self.generation += 1
        self.protocol_warning = ""
        self._reset_startup_probe()

    def feed(self, chunk: bytes) -> list[ParsedFrame]:
        self._observe_startup(chunk)
        self.buffer.extend(chunk)
        result = []
        pos = 0
        while len(self.buffer) - pos >= self.frame_size:
            if self.header and self.buffer[pos:pos + len(self.header)] != self.header:
                found = self.buffer.find(self.header, pos + 1)
                stop = found if found >= 0 else max(pos, len(self.buffer) - len(self.header) + 1)
                self.discarded += stop - pos
                # Opening an active stream can start in the middle of a frame.
                # Only report loss of alignment after a complete valid frame.
                if self.frames:
                    self.resyncs += 1
                    self.generation += 1
                pos = stop
                if found < 0:
                    break
            end = pos + self.frame_size
            if end > len(self.buffer):
                break
            if self.tail and self.buffer[end - len(self.tail):end] != self.tail:
                pos += 1
                self.discarded += 1
                if self.frames:
                    self.resyncs += 1
                    self.generation += 1
                continue
            begin = pos + len(self.header)
            payload = bytes(self.buffer[begin:begin + self.payload_size])
            data = np.frombuffer(payload, dtype=self.dtype)
            r = self.profile.rx
            data = data.reshape(r.samples_per_frame, r.channels) if r.layout == "interleaved" else data.reshape(r.channels, r.samples_per_frame).T
            result.append(ParsedFrame(data, self.generation))
            self.frames += 1
            pos = end
        if pos:
            del self.buffer[:pos]
        self._check_startup_length()
        return result


def make_frame(profile: Profile, data: np.ndarray) -> bytes:
    r = profile.rx
    data = np.asarray(data, dtype=dtype_for(r.data_type, profile.serial.byte_order))
    if data.shape != (r.samples_per_frame, r.channels):
        raise ValueError("模拟帧尺寸与配置不一致")
    if r.layout == "planar":
        data = data.T
    return marker_bytes(r.header, r.header_mode) + data.tobytes() + marker_bytes(r.terminator, r.terminator_mode)
