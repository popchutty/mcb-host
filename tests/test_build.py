from pathlib import Path
import subprocess
import sys
import tarfile
import zipfile

import pytest

from tools import build as builder


@pytest.fixture
def project(tmp_path, monkeypatch):
    (tmp_path / "profiles").mkdir()
    (tmp_path / "profiles" / "default.json").write_text('{"version": 2}', encoding="utf-8")
    (tmp_path / "README.md").write_text("SCI Scope", encoding="utf-8")
    (tmp_path / "start.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (tmp_path / "启动上位机.bat").write_text("@echo off\n", encoding="utf-8")
    (tmp_path / "dist" / "SCIScope").mkdir(parents=True)
    (tmp_path / "dist" / "SCIScope" / "old-file.txt").write_text("old", encoding="utf-8")

    def compile_bundle(arguments, **kwargs):
        assert arguments[:3] == [sys.executable, "-m", "PyInstaller"]
        bundle = Path(arguments[arguments.index("--distpath") + 1]) / "SCIScope"
        (bundle / "_internal").mkdir(parents=True)
        (bundle / ("SCIScope.exe" if sys.platform == "win32" else "SCIScope")).write_bytes(b"executable")

    monkeypatch.setattr(builder.subprocess, "run", compile_bundle)
    monkeypatch.setattr(builder, "patch_windows_runtime", lambda bundle: None)
    return tmp_path


def test_build_replaces_old_bundle_and_removes_intermediates(project):
    bundle = builder.build(project, archive=True)
    assert bundle == project / "dist" / "SCIScope"
    assert not (bundle / "old-file.txt").exists()
    assert (bundle / "profiles" / "default.json").is_file()
    assert (bundle / "README.md").read_text(encoding="utf-8") == "SCI Scope"
    assert not (project / "build").exists()
    if sys.platform == "win32":
        with zipfile.ZipFile(project / "dist" / "SCIScope-windows-x64.zip") as archive:
            assert "SCIScope/启动上位机.bat" in archive.namelist()
    else:
        with tarfile.open(project / "dist" / "SCIScope-linux-x64.tar.gz") as archive:
            assert "SCIScope/start.sh" in archive.getnames()
    # Rebuilding without --archive must also remove a prior release archive.
    builder.build(project)
    assert sorted(path.name for path in (project / "dist").iterdir()) == ["SCIScope"]


def test_compiler_failure_preserves_last_working_distribution(project, monkeypatch):
    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, ["PyInstaller"])
    monkeypatch.setattr(builder.subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        builder.build(project)
    assert (project / "dist" / "SCIScope" / "old-file.txt").read_text(encoding="utf-8") == "old"
    assert not (project / "build").exists()


def test_publish_failure_restores_previous_distribution(project, monkeypatch):
    original = Path.rename
    def fail_publish(path, target):
        if path == project / "build" / "release":
            raise OSError("publish failed")
        return original(path, target)
    monkeypatch.setattr(Path, "rename", fail_publish)
    with pytest.raises(OSError, match="publish failed"):
        builder.build(project)
    assert (project / "dist" / "SCIScope" / "old-file.txt").is_file()
    assert not (project / "build").exists()


def test_failed_restore_keeps_backup_and_next_build_recovers(project, monkeypatch):
    original = Path.rename
    def fail_publish_and_restore(path, target):
        if path.name in ("release", "previous-dist"):
            raise OSError("directory locked")
        return original(path, target)
    monkeypatch.setattr(Path, "rename", fail_publish_and_restore)
    with pytest.raises(OSError, match="directory locked"):
        builder.build(project)
    backup = project / "build" / "previous-dist" / "SCIScope" / "old-file.txt"
    assert backup.read_text(encoding="utf-8") == "old"
    monkeypatch.setattr(Path, "rename", original)
    assert builder.build(project).is_dir()
    assert not (project / "build").exists()


@pytest.mark.parametrize("target", ["root", "outside"])
def test_cleanup_refuses_project_root_and_external_paths(tmp_path, target):
    protected = tmp_path if target == "root" else tmp_path.parent
    with pytest.raises(ValueError, match="Refusing cleanup outside project"):
        builder.remove_tree(protected, tmp_path)
    assert tmp_path.is_dir()


def test_build_refuses_linked_output_directory(tmp_path):
    external = tmp_path / "external"
    external.mkdir()
    try:
        (tmp_path / "dist").symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip("Creating symlinks is unavailable for this account")
    with pytest.raises(ValueError, match="inside project"):
        builder.build(tmp_path)
    assert external.is_dir()
