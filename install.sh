#!/usr/bin/env bash
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd -- "$script_dir"
requirements="requirements.txt"
if [[ "${1:-}" == '--dev' ]]; then
    requirements="requirements-dev.txt"
    shift
fi
if (( $# )); then
    printf '%s\n' 'Usage: ./install.sh [--dev]' >&2
    exit 2
fi
venv_dir="${SCISCOPE_VENV:-$script_dir/.venv}"
python_command="${SCISCOPE_PYTHON:-python3}"
if [[ ! -x "$venv_dir/bin/python" ]]; then
    if [[ -e "$venv_dir" ]]; then
        printf '%s\n' 'Existing environment is incompatible. Set SCISCOPE_VENV to a new Linux environment directory.' >&2
        exit 1
    fi
    "$python_command" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else "Python 3.12 or newer is required")'
    "$python_command" -m venv "$venv_dir"
fi
"$venv_dir/bin/python" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else "Python 3.12 or newer is required")'
"$venv_dir/bin/python" -m pip install --disable-pip-version-check -r "$requirements"
printf '%s\n' 'Ready. Use ./start.sh to start SCI Scope.'
