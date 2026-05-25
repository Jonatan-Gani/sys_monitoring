#!/usr/bin/env sh
# Bootstrap for sys_monitoring on Linux / macOS.
# Creates a venv (unless SKIP_VENV=1), installs deps, then runs `sysmon init`.
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 not found. Install Python 3.10+ via your package manager." >&2
    exit 1
fi

if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
    echo "Python 3.10 or newer required (found $(python3 --version))." >&2
    exit 1
fi

if ! python3 -m pip --version >/dev/null 2>&1; then
    echo "pip not available. Install it (e.g. apt install python3-pip) and re-run." >&2
    exit 1
fi

if [ -z "${SKIP_VENV:-}" ]; then
    if [ ! -d .venv ]; then
        echo ">>> Creating virtualenv at .venv"
        python3 -m venv .venv
    fi
    # shellcheck disable=SC1091
    . .venv/bin/activate
    PY=.venv/bin/python
else
    PY=python3
fi

echo ">>> Installing dependencies"
"$PY" -m pip install --upgrade pip --quiet
"$PY" -m pip install -e . --quiet

echo ">>> Launching sysmon init"
"$PY" sysmon.py init "$@"
