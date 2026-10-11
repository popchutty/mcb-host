#!/usr/bin/env bash
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ -x "$script_dir/SCIScope" && -f "$script_dir/SCIScope" ]]; then
    exec "$script_dir/SCIScope" "$@"
fi
if [[ -x "$script_dir/dist/SCIScope/SCIScope" ]]; then
    exec "$script_dir/dist/SCIScope/SCIScope" "$@"
fi
venv_dir="${SCISCOPE_VENV:-$script_dir/.venv}"
if [[ "$venv_dir" != /* ]]; then venv_dir="$script_dir/$venv_dir"; fi
if [[ ! -x "$venv_dir/bin/python" ]]; then
    printf '%s\n' 'Python environment not found. Run ./install.sh first, or download a release.' >&2
    exit 1
fi
exec "$venv_dir/bin/python" "$script_dir/run.py" "$@"
