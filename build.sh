#!/usr/bin/env bash
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
venv_dir="${SCISCOPE_VENV:-$script_dir/.venv}"
if [[ "$venv_dir" != /* ]]; then venv_dir="$script_dir/$venv_dir"; fi
python_command="$venv_dir/bin/python"
if [[ ! -x "$python_command" ]]; then
    python_command="${SCISCOPE_PYTHON:-python3}"
fi
exec "$python_command" "$script_dir/tools/build.py" "$@"
