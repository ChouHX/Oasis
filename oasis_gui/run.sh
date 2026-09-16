#!/usr/bin/env bash
# Launch the Oasis Live '27 registration console.
set -euo pipefail
cd "$(dirname "$0")"
exec python3 app.py "$@"
