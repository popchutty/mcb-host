from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import queue
import threading
import time

import h5py
import numpy as np
from scipy.io import savemat

from .config import Profile, dtype_for
from .protocol import physical_values

FRAME_DTYPE = np.dtype([("received_ns", "<i8"), ("sample_start", "<i8"),
                        ("rows", "<i4"), ("transition", "u1"), ("segment", "<i4")])


@dataclass
class FrameRecord:
    data: np.ndarray
    received_ns: int
    sample_start: int
    segment: int
    transition: bool
    profile: Profile


class SessionRecorder(threading.Thread):
    """Only this thread accesses the writable HDF5 handles."""
    def __init__(self, root: str | Path, profile: Profile, *, rotate_seconds: float = 600):
        super().__init__(name="record-writer", daemon=False)
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=False)
        self.profile = profile.clone()
        self.rotate_seconds = rotate_seconds
        self.items: queue.Queue = queue.Queue(maxsize=256)
        self.stopping = threading.Event()
        self.failed = ""
        self.pending_bytes = 0
        self.lock = threading.Lock()
        self.written_rows = 0
        self.manifest = {"version": 1, "created": datetime.now().astimezone().isoformat(), "complete": False,
                         "error": "", "profile": self.profile.to_dict(), "segments": []}

    def submit(self, frame: FrameRecord) -> bool:
        with self.lock:
            if self.stopping.is_set() or self.failed:
                return False
            if self.pending_bytes + frame.data.nbytes > 32 * 1024**2:
                self.failed = "记录队列超过 32 MiB，记录不完整；监看继续"
                self.stopping.set()
                return False
            try:
                self.items.put_nowait(("frame", frame))
                self.pending_bytes += frame.data.nbytes
                return True
            except queue.Full:
                self.failed = "记录队列已满，记录不完整；监看继续"
                self.stopping.set()
                return False

    def event(self, event: dict) -> None:
        if self.stopping.is_set():
            return
        try:
            self.items.put_nowait(("event", dict(event, received_ns=time.time_ns())))
        except queue.Full:
            self.failed = "记录事件队列已满，记录不完整"
            self.stopping.set()

    def stop(self) -> None:
        self.stopping.set()

    def _manifest(self) -> None:
        temp = self.root / "session.json.tmp"
        temp.write_text(json.dumps(self.manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.root / "session.json")

    def run(self) -> None:
        handle = None
        current_segment = None
        raw = frames = None
        last_flush = time.monotonic()
        try:
            self._manifest()
            with (self.root / "events.jsonl").open("a", encoding="utf-8") as events:
                while not self.stopping.is_set() or not self.items.empty():
                    try:
                        kind, item = self.items.get(timeout=0.1)
                    except queue.Empty:
                        kind, item = "idle", None
                    if kind == "event":
                        events.write(json.dumps(item, ensure_ascii=False) + "\n")
                        events.flush()
                    elif kind == "frame":
                        frame: FrameRecord = item
                        with self.lock:
                            self.pending_bytes -= frame.data.nbytes
                        rotate = handle is not None and (len(raw) + len(frame.data)) / frame.profile.rx.sample_rate > self.rotate_seconds
                        if handle is None or current_segment != frame.segment or rotate:
                            if handle is not None:
                                handle.attrs["closed_cleanly"] = True
                                handle.flush()
                                handle.close()
                            filename = f"segment_{len(self.manifest['segments']) + 1:04d}.h5"
                            handle = h5py.File(self.root / filename, "w")
                            handle.attrs["version"] = 1
                            handle.attrs["profile_json"] = json.dumps(frame.profile.to_dict(), ensure_ascii=False)
                            handle.attrs["semantic_source"] = "configured_channel_metadata"
                            handle.attrs["closed_cleanly"] = False
                            raw = handle.create_dataset("raw", (0, frame.profile.rx.channels), maxshape=(None, frame.profile.rx.channels),
                                                        dtype=frame.data.dtype, chunks=(min(frame.profile.rx.samples_per_frame * 4, 16384), frame.profile.rx.channels))
                            frames = handle.create_dataset("frames", (0,), maxshape=(None,), dtype=FRAME_DTYPE, chunks=(256,))
                            current_segment = frame.segment
                            self.manifest["segments"].append({"file": filename, "segment": frame.segment, "rows": 0,
                                                              "first_received_ns": frame.received_ns, "profile_name": frame.profile.name})
                            self._manifest()
                        begin = len(raw)
                        raw.resize(begin + len(frame.data), axis=0)
                        raw[begin:] = frame.data
                        frames.resize(len(frames) + 1, axis=0)
                        frames[-1] = (frame.received_ns, frame.sample_start, len(frame.data), frame.transition, frame.segment)
                        self.written_rows += len(frame.data)
                        self.manifest["segments"][-1]["rows"] = len(raw)
                    if time.monotonic() - last_flush >= 1:
                        if handle is not None:
                            handle.flush()
                        self._manifest()
                        last_flush = time.monotonic()
                self.manifest["complete"] = not bool(self.failed)
        except Exception as exc:
            self.failed = f"写盘失败：{exc}"
            self.stopping.set()
            self.manifest["complete"] = False
        finally:
            if handle is not None:
                try:
                    handle.attrs["closed_cleanly"] = not bool(self.failed)
                    handle.flush()
                    handle.close()
                except Exception as exc:
                    self.failed = self.failed or f"关闭记录失败：{exc}"
            self.manifest["error"] = self.failed
            self.manifest["complete"] = self.manifest["complete"] and not bool(self.failed)
            try:
                self._manifest()
            except Exception as exc:
                self.failed = self.failed or f"保存会话索引失败：{exc}"


class SegmentReader:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        with h5py.File(self.path, "r") as handle:
            self.profile = Profile.from_dict(json.loads(handle.attrs["profile_json"]))
            self.rows = len(handle["raw"])
            self.closed_cleanly = bool(handle.attrs.get("closed_cleanly", False))
        self.duration = self.rows / self.profile.rx.sample_rate

    def read(self, start: float = 0, end: float | None = None, channels: list[int] | None = None,
             *, physical: bool = True) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        r = self.profile.rx
        lo = max(0, min(self.rows, round(start * r.sample_rate)))
        hi = self.rows if end is None else max(lo, min(self.rows, round(end * r.sample_rate)))
        with h5py.File(self.path, "r") as handle:
            data = handle["raw"][lo:hi]
            # Rows without a committed frame index (e.g. failed final write) are invalid.
            valid = np.zeros(hi - lo, dtype=bool)
            offset = 0
            for frame in handle["frames"]:
                next_offset = offset + int(frame["rows"])
                if offset < hi and next_offset > lo:
                    valid[max(0, offset - lo):min(hi - lo, next_offset - lo)] = not bool(frame["transition"])
                offset = next_offset
                if offset >= hi:
                    break
        values = physical_values(data, self.profile, physical)
        if channels is not None:
            values = values[:, channels]
        return np.arange(lo, hi, dtype=np.float64) / r.sample_rate, values, valid


def session_files(path: str | Path) -> list[Path]:
    path = Path(path)
    if path.is_file() and path.suffix == ".h5":
        return [path]
    manifest = path / "session.json"
    if manifest.exists():
        data = json.loads(manifest.read_text(encoding="utf-8"))
        return [path / s["file"] for s in data["segments"]]
    return sorted(path.glob("segment_*.h5"))


def export_segment(reader: SegmentReader, destination: str | Path, kind: str, start: float = 0,
                   end: float | None = None, channels: list[int] | None = None,
                   *, mat_limit: int = 256 * 1024**2, progress=None) -> list[Path]:
    destination = Path(destination)
    p = reader.profile
    channels = list(range(p.rx.channels)) if channels is None else channels
    if not channels:
        raise ValueError("至少选择一个通道")
    lo = max(0, min(reader.rows, round(start * p.rx.sample_rate)))
    hi = reader.rows if end is None else max(lo, min(reader.rows, round(end * p.rx.sample_rate)))
    if hi == lo:
        raise ValueError("导出区间没有数据")
    selected = [p.rx.channel_info[c] for c in channels]
    metadata = {"source": str(reader.path), "profile": p.to_dict(), "channels": channels,
                "time_basis": "segment_sample_index", "semantic_source": "configured_channel_metadata"}
    created = []
    if kind == "csv":
        import csv
        with destination.open("w", newline="", encoding="utf-8-sig") as out:
            writer = csv.writer(out)
            writer.writerow(["segment", "time_s", "valid"] + [f"{c.name}[{c.unit}]" for c in selected])
            for first in range(lo, hi, 65536):
                last = min(hi, first + 65536)
                t, data, valid = reader.read(first / p.rx.sample_rate, last / p.rx.sample_rate, channels)
                writer.writerows((reader.path.stem, f"{ti:.12g}", int(ok), *row) for ti, ok, row in zip(t, valid, data))
                if progress:
                    progress((last - lo) / (hi - lo))
        created.append(destination)
        meta_path = destination.with_suffix(".metadata.json")
        meta_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        created.append(meta_path)
    elif kind == "mat":
        # Float64 values + time + validity; leave space for MAT metadata and cell arrays.
        rows_per_part = max(1, (mat_limit - 65536) // (8 * (len(channels) + 1) + 1))
        total = (hi - lo + rows_per_part - 1) // rows_per_part
        for part, first in enumerate(range(lo, hi, rows_per_part), 1):
            last = min(hi, first + rows_per_part)
            t, data, valid = reader.read(first / p.rx.sample_rate, last / p.rx.sample_rate, channels)
            output = destination if total == 1 else destination.with_name(f"{destination.stem}_{part:04d}.mat")
            savemat(output, {"time_s": t[:, None], "data": data, "valid": valid[:, None],
                            "sample_rate": p.rx.sample_rate, "channel_names": np.asarray([c.name for c in selected], dtype=object),
                            "units": np.asarray([c.unit for c in selected], dtype=object), "profile_json": json.dumps(metadata, ensure_ascii=False),
                            "segment": reader.path.stem}, do_compression=False, long_field_names=True)
            created.append(output)
            if progress:
                progress((last - lo) / (hi - lo))
    else:
        raise ValueError("导出格式必须为 csv 或 mat")
    return created
