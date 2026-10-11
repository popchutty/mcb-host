"""Measure GUI copy/conversion/render work using synthetic data, without serial I/O."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
from PySide6 import QtWidgets as W

from sciscope import __version__
from sciscope.config import Channel, Profile
from sciscope.protocol import physical_values
from sciscope.ui import ScopeWidget, configure_application


def main():
    app = W.QApplication([])
    configure_application(app)
    results = []
    for channels in (2, 8, 14):
        profile = Profile()
        profile.rx.channels = channels
        profile.rx.channel_info = [Channel(f"CH{i + 1}") for i in range(channels)]
        scope = ScopeWidget()
        scope.configure(profile)
        scope.resize(1400, 850)
        scope.show()
        app.processEvents()
        try:
            for seconds in (1, 30):
                rows = int(seconds * profile.rx.sample_rate)
                t = np.arange(rows) / profile.rx.sample_rate
                raw = np.empty((rows, channels), dtype=np.float32)
                for channel in range(channels):
                    raw[:, channel] = np.sin(2 * np.pi * (13 + channel) * t)
                scope.window.setValue(seconds)
                durations = []
                for iteration in range(12):
                    began = time.perf_counter()
                    copied = raw.copy()  # Include the GUI-side ring snapshot copy.
                    data = physical_values(copied, profile)
                    scope.update_data(t + iteration * 0.03, data)
                    app.processEvents()  # Include Qt painting and queued resize work.
                    if iteration >= 2:
                        durations.append((time.perf_counter() - began) * 1000)
                results.append({"channels": channels, "window_seconds": seconds, "raw_rows": rows,
                                "median_ms": round(float(np.median(durations)), 2),
                                "p95_ms": round(float(np.percentile(durations, 95)), 2),
                                "max_draw_points_per_channel": max(len(curve.getData()[0]) for curve in scope.curves.values())})
        finally:
            scope.close()
            scope.deleteLater()
            app.processEvents()
    path = Path(__file__).resolve().parents[1] / "artifacts" / ("plotting_v" + __version__.replace(".", "_")) / "report.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": __version__, "mode": "synthetic_offscreen", "results": results}, indent=2), encoding="utf-8")
    print(path)
    print(json.dumps(results))


if __name__ == "__main__":
    main()
