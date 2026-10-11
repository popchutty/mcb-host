"""Check a packaged application's simulated capture without opening a serial port."""
from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile

import h5py
import numpy as np


def main():
    root = Path(__file__).resolve().parents[1]
    executable = root / "dist" / "SCIScope" / ("SCIScope.exe" if os.name == "nt" else "SCIScope")
    artifacts = root / "artifacts"
    artifacts.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".smoke-", dir=artifacts) as temporary:
        output = Path(temporary) / "capture"
        environment = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}
        result = subprocess.run([str(executable), "--smoke-check", str(output)], env=environment,
                                capture_output=True, text=True, timeout=60)
        if result.returncode:
            raise RuntimeError(f"Packaged app exited with {result.returncode}: {result.stderr}")
        report = json.loads((output / "smoke.json").read_text(encoding="utf-8"))
        manifest = json.loads((output / "session" / "session.json").read_text(encoding="utf-8"))
        assert report["passed"] and manifest["complete"] and not report["error"]
        rows = 0
        for segment in manifest["segments"]:
            with h5py.File(output / "session" / segment["file"], "r") as handle:
                assert handle.attrs["closed_cleanly"]
                assert np.isfinite(handle["raw"][:]).all()
                assert not handle["frames"]["transition"][:].any()
                assert int(handle["frames"]["rows"][:].sum()) == len(handle["raw"])
                rows += len(handle["raw"])
        assert rows == report["rows_written"] > 0
        print(json.dumps(report))


if __name__ == "__main__":
    main()
