import argparse
import os
from pathlib import Path
import sys
import tempfile
import traceback


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--smoke-check", help="Run an offscreen simulated capture and save a QA report")
    args = parser.parse_args()
    if args.smoke_check:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from sciscope.ui import run_app
    return run_app(args.smoke_check)


if __name__ == "__main__":
    try:
        code = main()
    except Exception:
        details = traceback.format_exc()
        root = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
        try:
            (root / "startup-error.log").write_text(details, encoding="utf-8")
        except OSError:
            root = Path(tempfile.gettempdir())
            try:
                (root / "SCIScope-startup-error.log").write_text(details, encoding="utf-8")
            except OSError:
                pass
        if "--smoke-check" not in sys.argv and os.name == "nt":
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, f"Startup failed. See the error log in:\n{root}", "SCI Scope", 0x10)
        if sys.stderr is not None:
            sys.stderr.write(details)
        code = 1
    raise SystemExit(code)
