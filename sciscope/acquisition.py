from __future__ import annotations

import itertools
from pathlib import Path
import queue
import threading
import time

import numpy as np
from PySide6.QtCore import QThread, Signal
import serial

from .config import Profile, dtype_for
from .display import DEFAULT_BUFFER_POINTS, ScopeSettings
from .protocol import FrameParser, make_frame
from .storage import FrameRecord, SessionRecorder


class RingBuffer:
    def __init__(self, profile: Profile, seconds: float | None = None, *, buffer_points: int = DEFAULT_BUFFER_POINTS):
        r = profile.rx
        dt = dtype_for(r.data_type, profile.serial.byte_order)
        if seconds is not None:
            buffer_points = max(r.samples_per_frame, int(r.sample_rate * seconds))
        self.capacity = ScopeSettings(buffer_points).plan(profile)[0]
        self.data = np.empty((self.capacity, r.channels), dtype=dt)
        self.valid = np.empty(self.capacity, dtype=bool)
        self.total = self.count = 0
        self.lock = threading.Lock()

    def clear(self) -> None:
        with self.lock:
            self.total = self.count = 0

    def resize(self, capacity: int) -> None:
        """Retain the newest available rows and their absolute sample indices."""
        if capacity == self.capacity:
            return
        data = np.empty((capacity, self.data.shape[1]), dtype=self.data.dtype)
        valid = np.empty(capacity, dtype=bool)
        with self.lock:
            count = min(self.count, capacity)
            start = self.total - count
            source, target, remaining = start % self.capacity, start % capacity, count
            while remaining:
                size = min(remaining, self.capacity - source, capacity - target)
                data[target:target + size] = self.data[source:source + size]
                valid[target:target + size] = self.valid[source:source + size]
                source, target = (source + size) % self.capacity, (target + size) % capacity
                remaining -= size
            self.data, self.valid, self.capacity, self.count = data, valid, capacity, count

    def append(self, values: np.ndarray, valid: bool = True) -> None:
        with self.lock:
            if len(values) > self.capacity:
                skipped = len(values) - self.capacity
                self.total += skipped
                values = values[-self.capacity:]
            pos = self.total % self.capacity
            first = min(len(values), self.capacity - pos)
            self.data[pos:pos + first] = values[:first]
            self.valid[pos:pos + first] = valid
            if first < len(values):
                self.data[:len(values) - first] = values[first:]
                self.valid[:len(values) - first] = valid
            self.total += len(values)
            self.count = min(self.capacity, self.count + len(values))

    def snapshot(self, max_rows: int | None = None) -> tuple[int, np.ndarray, np.ndarray]:
        with self.lock:
            count = self.count if max_rows is None else min(self.count, max_rows)
            start = self.total - count
            return self._snapshot_locked(start, self.total)

    def bounds(self) -> tuple[int, int]:
        with self.lock:
            return self.total - self.count, self.total

    def snapshot_range(self, start: int, end: int) -> tuple[int, np.ndarray, np.ndarray]:
        """Copy only available samples in the absolute, half-open row interval."""
        with self.lock:
            start = max(self.total - self.count, min(self.total, start))
            end = max(start, min(self.total, end))
            return self._snapshot_locked(start, end)

    def _snapshot_locked(self, start, end):
        count = end - start
        pos = start % self.capacity
        first = min(count, self.capacity - pos)
        if first == count:
            data, valid = self.data[pos:pos + count].copy(), self.valid[pos:pos + count].copy()
        else:
            data = np.concatenate((self.data[pos:pos + first], self.data[:count - first]))
            valid = np.concatenate((self.valid[pos:pos + first], self.valid[:count - first]))
        return start, data, valid


class DataHub:
    def __init__(self, profile: Profile, *, buffer_points: int = DEFAULT_BUFFER_POINTS):
        self.profile = profile.clone()
        self.ring = RingBuffer(profile, buffer_points=buffer_points)
        self.recorder: SessionRecorder | None = None
        self.lock = threading.Lock()
        self.frames = self.bytes_received = self.resyncs = 0
        self.protocol_warning = ""
        self.segment = self.segment_rows = 0
        self.generation = 0
        self.transition_frames = 0
        self.started = time.monotonic()

    def resize_buffer(self, points: int) -> None:
        with self.lock:
            ScopeSettings(points).validate(self.profile)
            self.ring.resize(points)

    def reception_warning(self, now: float | None = None) -> str:
        if self.protocol_warning:
            return self.protocol_warning
        now = time.monotonic() if now is None else now
        # Allow several frame periods for deliberately slow protocols.
        wait = max(3.0, 3 * self.profile.rx.samples_per_frame / self.profile.rx.sample_rate)
        if not self.frames and now - self.started >= wait:
            return "未收到有效帧，请检查串口连接、波特率及接收协议配置。"
        return ""

    def accept(self, parsed) -> None:
        with self.lock:
            if parsed.generation != self.generation:
                self.generation = parsed.generation
                self.segment += 1
                self.segment_rows = 0
                self.ring.clear()
            transition = self.transition_frames > 0
            self.transition_frames = max(0, self.transition_frames - 1)
            self.ring.append(parsed.data, not transition)
            frame = FrameRecord(parsed.data, time.time_ns(), self.segment_rows, self.segment, transition, self.profile)
            if self.recorder:
                self.recorder.submit(frame)
            self.segment_rows += len(parsed.data)
            self.frames += 1

    def update_metadata(self, profile: Profile, transition_frames: int = 0) -> None:
        """Explicit metadata change; never infer channel semantics from TX bytes."""
        if (profile.rx.channels, profile.rx.samples_per_frame, profile.rx.data_type, profile.rx.layout, profile.rx.sample_rate, profile.serial.byte_order) != (
                self.profile.rx.channels, self.profile.rx.samples_per_frame, self.profile.rx.data_type, self.profile.rx.layout, self.profile.rx.sample_rate, self.profile.serial.byte_order):
            raise ValueError("连接期间只能更新通道说明，不能改变接收帧结构")
        with self.lock:
            self.profile = profile.clone()
            self.segment += 1
            self.segment_rows = 0
            self.transition_frames = transition_frames
            self.ring.clear()
            if self.recorder:
                self.recorder.event({"type": "channel_metadata", "channels": [c.name for c in profile.rx.channel_info]})

    def record_event(self, event: dict) -> None:
        with self.lock:
            if self.recorder:
                self.recorder.event(event)

    def start_recording(self, path: str | Path) -> SessionRecorder:
        with self.lock:
            if self.recorder and self.recorder.is_alive():
                raise ValueError("正在记录")
            recorder = SessionRecorder(path, self.profile)
            self.recorder = recorder
            recorder.start()
            return recorder

    def stop_recording(self) -> SessionRecorder | None:
        with self.lock:
            recorder, self.recorder = self.recorder, None
            if recorder:
                recorder.stop()
            return recorder


class AcquisitionWorker(QThread):
    error = Signal(str)
    connected = Signal(str)
    command_sent = Signal(str)
    tagged_command_sent = Signal(str, str)

    def __init__(self, profile: Profile, hub: DataHub, simulation: bool = False, parent=None):
        super().__init__(parent)
        self.profile = profile.clone()
        self.hub = hub
        self.simulation = simulation
        self.commands: queue.PriorityQueue = queue.PriorityQueue(maxsize=128)
        self.counter = itertools.count()
        self.stopping = threading.Event()
        self.parser = FrameParser(profile)
        self.sim_rows = 0

    def send(self, payload: bytes, *, urgent: bool = False, metadata: Profile | None = None, transition_frames: int = 0, origin: str = "") -> None:
        try:
            self.commands.put_nowait((0 if urgent else 1, next(self.counter), payload, metadata.clone() if metadata else None, transition_frames, origin))
        except queue.Full as exc:
            raise ValueError("下发队列已满") from exc

    def stop(self) -> None:
        self.stopping.set()

    def _commands(self, port) -> None:
        # Explicit urgent actions supersede queued updates. Normal frames are FIFO.
        for _ in range(128):
            try:
                priority, _, payload, metadata, transition_frames, origin = self.commands.get_nowait()
            except queue.Empty:
                break
            if priority == 0:
                while not self.commands.empty():
                    pending = self.commands.get_nowait()
                    # Preserve the latest urgent action while discarding old updates.
                    if pending[0] == 0:
                        _, _, payload, metadata, transition_frames, origin = pending
            if port is not None:
                sent = port.write(payload)
                if sent != len(payload):
                    raise IOError("串口未完整发送命令")
            if metadata is not None:
                self.hub.update_metadata(metadata, transition_frames=transition_frames)
            self.hub.record_event({"type": "tx", "hex": payload.hex(" "), "simulated": self.simulation, "origin": origin})
            self.command_sent.emit(payload.hex(" ").upper())
            self.tagged_command_sent.emit(origin, payload.hex(" ").upper())

    def run(self) -> None:
        port = None
        try:
            if not self.simulation:
                s = self.profile.serial
                port = serial.Serial(s.port, s.baudrate, bytesize=s.bytesize, parity=s.parity,
                                     stopbits=s.stopbits, timeout=0.005, write_timeout=0.5,
                                     xonxoff=False, rtscts=False, dsrdtr=False)
            self.hub.started = time.monotonic()
            self.connected.emit("模拟数据源（不会控制硬件）" if self.simulation else self.profile.serial.port)
            due = time.monotonic()
            while not self.stopping.is_set():
                self._commands(port)
                if self.simulation:
                    r = self.profile.rx
                    t = (np.arange(r.samples_per_frame) + self.sim_rows) / r.sample_rate
                    values = np.stack([0.5 * np.sin(2 * np.pi * (i + 1) * 20 * t) for i in range(r.channels)], axis=1)
                    dt = dtype_for(r.data_type, self.profile.serial.byte_order)
                    if dt.kind in "iu":
                        values = np.round((values + (0.5 if dt.kind == "u" else 0)) * 100)
                    chunk = make_frame(self.profile, values)
                    self.sim_rows += r.samples_per_frame
                    due += r.samples_per_frame / r.sample_rate
                else:
                    chunk = port.read(max(1, min(port.in_waiting, 65536)))
                self.hub.bytes_received += len(chunk)
                for frame in self.parser.feed(chunk):
                    self.hub.accept(frame)
                self.hub.resyncs = self.parser.resyncs
                self.hub.protocol_warning = self.parser.protocol_warning
                if self.simulation:
                    self.stopping.wait(max(0, due - time.monotonic()))
        except Exception as exc:
            self.error.emit(f"采集停止：{exc}")
        finally:
            if port is not None:
                port.close()
