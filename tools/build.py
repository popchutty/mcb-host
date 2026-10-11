"""Build on the current OS, publishing only the latest bundle to dist/."""
from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def remove_tree(path: Path, root: Path) -> None:
    resolved = path.resolve()
    if resolved == root.resolve() or not resolved.is_relative_to(root.resolve()):
        raise ValueError(f"Refusing cleanup outside project: {path}")
    if path.is_symlink() or path.is_junction():
        raise ValueError(f"Refusing cleanup of a linked directory: {path}")
    if path.exists():
        shutil.rmtree(path)


def patch_windows_runtime(bundle: Path) -> None:
    # Match Qt's MSVC runtime rather than the older DLLs bundled with Python.
    specification = importlib.util.find_spec("PySide6")
    qt = Path(specification.origin).parent
    internal = bundle / "_internal"
    for name in ("vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll", "msvcp140_1.dll",
                 "msvcp140_2.dll", "msvcp140_codecvt_ids.dll", "concrt140.dll"):
        if (qt / name).is_file():
            shutil.copy2(qt / name, internal / name)
    # This pinned Qt build uses Windows ICU; MATLAB's PATH can provide an
    # unrelated DLL whose exports do not match Qt. Do not copy that DLL.
    for library in internal.iterdir():
        if library.is_file() and (library.name == "icuuc.dll" or library.name.startswith("icudt")):
            library.unlink()


def build(root: Path = ROOT, archive: bool = False) -> Path:
    if sys.version_info < (3, 12):
        raise RuntimeError("Python 3.12 or newer is required")
    if sys.platform not in ("win32", "linux"):
        raise RuntimeError("Build on Windows or Linux; cross-compilation is not supported")
    root = root.resolve()
    work, destination = root / "build", root / "dist"
    for path in (work, destination):
        if path.is_symlink() or path.is_junction() or not path.resolve().is_relative_to(root):
            raise ValueError(f"Build directory must remain inside project: {path}")
    previous = work / "previous-dist"
    if previous.exists() and not destination.exists():
        previous.rename(destination)
    remove_tree(work, root)
    release = work / "release"
    try:
        subprocess.run([
            sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed",
            "--name", "SCIScope", "--distpath", str(release), "--workpath", str(work / "work"),
            "--specpath", str(work), "--exclude-module", "pyqtgraph.examples",
            "--exclude-module", "PySide6.QtWebEngineCore", "--exclude-module", "PySide6.QtWebEngineWidgets",
            "--exclude-module", "PySide6.QtPdf", str(root / "run.py"),
        ], cwd=root, check=True)
        bundle = release / "SCIScope"
        executable = bundle / ("SCIScope.exe" if sys.platform == "win32" else "SCIScope")
        if not executable.is_file():
            raise RuntimeError(f"Build did not produce {executable}")
        if sys.platform == "win32":
            patch_windows_runtime(bundle)
        shutil.copy2(root / "README.md", bundle / "README.md")
        shutil.copytree(root / "profiles", bundle / "profiles")
        launcher = "启动上位机.bat" if sys.platform == "win32" else "start.sh"
        shutil.copy2(root / launcher, bundle / launcher)
        if archive:
            platform = "windows" if sys.platform == "win32" else "linux"
            format = "zip" if platform == "windows" else "gztar"
            shutil.make_archive(str(release / f"SCIScope-{platform}-x64"), format,
                                root_dir=release, base_dir="SCIScope")
        if destination.exists():
            destination.rename(previous)
        try:
            release.rename(destination)
        except OSError:
            if previous.exists():
                previous.rename(destination)
            raise
        return destination / "SCIScope"
    finally:
        if previous.exists() and not destination.exists():
            print(f"Previous bundle retained for recovery: {previous}", file=sys.stderr)
        else:
            remove_tree(work, root)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", action="store_true", help="Also create a Windows ZIP or Linux tar.gz")
    args = parser.parse_args()
    try:
        bundle = build(archive=args.archive)
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as error:
        print(f"Build failed: {error}\nInstall developer dependencies first (install.ps1 -Dev / install.sh --dev).", file=sys.stderr)
        return 1
    print(f"Bundle ready: {bundle}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
