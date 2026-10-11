"""Exercise simulated 20 kHz frames through parser, ring, plots and disk.

--speed 1 is a real-time soak; a higher value tests one hour of samples faster.
No serial port is opened.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import h5py
import numpy as np
from PySide6 import QtGui, QtWidgets as W

from sciscope.acquisition import DataHub
from sciscope.config import Channel, Profile
from sciscope.protocol import FrameParser, make_frame, physical_values
from sciscope.storage import session_files
from sciscope.ui import ScopeWidget, configure_application


class ProcessMemory(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]


def memory_mb():
    if os.name != "nt":
        return None
    counters = ProcessMemory()
    counters.cb = ctypes.sizeof(counters)
    ctypes.windll.psapi.GetProcessMemoryInfo(ctypes.c_void_p(-1), ctypes.byref(counters), counters.cb)
    return counters.WorkingSetSize / 1024**2


def samples(first, n, channels):
    # Exact, deterministic integer-valued float32 samples, including rare spikes.
    index = np.arange(first, first + n, dtype=np.int64)[:, None]
    values = ((index + np.arange(channels)[None, :] * 17) % 4096 - 2048).astype(np.float32) / 2048
    values[index[:, 0] % 100003 == 0, :] = 10
    return values


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=3600)
    parser.add_argument("--speed", type=float, default=1)
    parser.add_argument("--channels", type=int, default=14)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    p = Profile()
    p.rx.channels = args.channels
    p.rx.channel_info = [Channel(f"CH{i + 1}") for i in range(args.channels)]
    p.validate()
    app = W.QApplication([])
    configure_application(app)
    scope = ScopeWidget()
    scope.configure(p)
    scope.resize(1350, 850)
    scope.show()
    hub = DataHub(p)
    recorder = hub.start_recording(args.output / "session")
    frame_parser = FrameParser(p)
    frames_target = round(args.seconds * p.rx.sample_rate / p.rx.samples_per_frame)
    began = time.monotonic()
    next_draw = began
    next_progress = began
    plots = 0
    memory = []
    capture_rows = 0
    try:
        for index in range(frames_target):
            # Bound replay pressure without ever dropping record frames.
            while recorder.pending_bytes > 4 * 1024**2:
                if recorder.failed:
                    raise RuntimeError(recorder.failed)
                app.processEvents()
                time.sleep(0.001)
            frame = make_frame(p, samples(index * p.rx.samples_per_frame, p.rx.samples_per_frame, p.rx.channels))
            # Fragmented delivery across arbitrary read boundaries.
            split = (index * 97) % (len(frame) - 1) + 1
            for parsed in frame_parser.feed(frame[:split]) + frame_parser.feed(frame[split:]):
                hub.accept(parsed)
            capture_rows += p.rx.samples_per_frame
            now = time.monotonic()
            if now >= next_draw:
                start, raw, valid = hub.ring.snapshot(int(scope.window.value() * p.rx.sample_rate))
                t = (np.arange(len(raw)) + start) / p.rx.sample_rate
                scope.update_data(t, physical_values(raw, p), valid)
                app.processEvents()
                plots += 1
                next_draw = now + 1 / 30
            if now >= next_progress:
                memory.append({"wall_seconds": now - began, "sample_seconds": capture_rows / p.rx.sample_rate, "rss_mb": memory_mb()})
                (args.output / "progress.json").write_text(json.dumps(memory[-1]), encoding="utf-8")
                print(json.dumps(memory[-1]), flush=True)
                next_progress = now + 10
            wait = began + (index + 1) * p.rx.samples_per_frame / p.rx.sample_rate / args.speed - time.monotonic()
            if wait > 0:
                app.processEvents()
                time.sleep(min(wait, 0.03))
        scope.grab().save(str(args.output / "scope.png"))
    finally:
        hub.stop_recording()
        recorder.join(60)
    if recorder.is_alive() or recorder.failed:
        raise RuntimeError(recorder.failed or "writer did not terminate")
    # Validate every persisted sample with bounded memory.
    offset = 0
    for file in session_files(recorder.root):
        with h5py.File(file, "r") as handle:
            raw = handle["raw"]
            for begin in range(0, len(raw), 60000):
                actual = raw[begin:begin + 60000]
                expected = samples(offset + begin, len(actual), p.rx.channels)
                np.testing.assert_array_equal(actual, expected)
            offset += len(raw)
    assert offset == capture_rows
    report = {"status": "passed", "mode": "real_time" if args.speed == 1 else "accelerated",
              "requested_speed": args.speed, "sample_seconds": capture_rows / p.rx.sample_rate,
              "wall_seconds": time.monotonic() - began, "channels": p.rx.channels, "sample_rate": p.rx.sample_rate,
              "frames": hub.frames, "rows": offset, "plot_updates": plots, "parser_resyncs": frame_parser.resyncs,
              "all_samples_verified": True, "memory_samples": memory, "final_rss_mb": memory_mb()}
    (args.output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "memory_samples"}), flush=True)
    scope.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
