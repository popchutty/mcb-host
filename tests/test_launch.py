from pathlib import Path
import os
import shutil
import subprocess
import sys

import pytest


@pytest.mark.skipif(sys.platform != "linux", reason="Linux launcher integration")
@pytest.mark.parametrize("packaged", [False, True])
def test_linux_launcher_handles_spaces_other_working_directory_and_arguments(tmp_path, packaged):
    project = tmp_path / "中文 project with spaces"
    project.mkdir()
    source = Path(__file__).resolve().parents[1] / "start.sh"
    shutil.copy2(source, project / "start.sh")
    if packaged:
        executable = project / "SCIScope"
    else:
        executable = project / "custom env" / "bin" / "python"
        executable.parent.mkdir(parents=True)
    executable.write_bytes(b'#!/bin/sh\nprintf "%s\\n" "$@"\n')
    executable.chmod(0o755)
    environment = {**os.environ, "SCISCOPE_VENV": "custom env"}
    result = subprocess.run(["bash", str(project / "start.sh"), "--example", "value with spaces"],
                            cwd=tmp_path, env=environment, capture_output=True, text=True, check=True)
    expected = ["--example", "value with spaces"]
    if not packaged:
        expected.insert(0, str(project / "run.py"))
    assert result.stdout.splitlines() == expected
