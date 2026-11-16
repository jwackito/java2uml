#!/usr/bin/env bash
#
# Create (or reuse) a project-local virtualenv, install requirements, and run
# java2uml. Nothing is installed globally.
#
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="${JAVA2UML_VENV:-$PROJECT_DIR/.venv}"
PYTHON="$VENV/bin/python"

if [ ! -x "$PYTHON" ]; then
    echo "java2uml: creating virtualenv at $VENV" >&2
    python3 -m venv "$VENV"
fi

if ! "$PYTHON" -c "import javalang" >/dev/null 2>&1; then
    echo "java2uml: installing requirements from $PROJECT_DIR/requirements.txt" >&2
    # Use "python -m pip" rather than the venv's pip script: some filesystems
    # (for example /home with noexec) refuse to execute scripts inside a venv.
    "$PYTHON" -m pip install --quiet -r "$PROJECT_DIR/requirements.txt"
fi

exec "$PYTHON" "$PROJECT_DIR/java2uml.py" "$@"
