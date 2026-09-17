#!/usr/bin/env bash
# Container entrypoint: make the mounted account pool writable, then drop root.
#
# The host's ./data arrives owned by whoever created it there (usually uid
# 1000), while the image runs as uid 10001 - SQLite then fails with "attempt to
# write a readonly database" after the container has already reported healthy,
# which reads like a random crash. Fix the ownership first, then drop root.
#
# Started with `docker run --user`, we are not root, so the chown is skipped
# and the caller owns that problem.
set -euo pipefail

DATA_DIR="$(dirname "${OASIS_DB:-/data/oasis.db}")"

# ---------------------------------------------------------------- HOME -------
# Chromium writes its profile, its crash database and its first-run state under
# $HOME. With a read-only $HOME the full chromium dies during startup with
# SIGTRAP - reported as "exited immediately (code -5)" - before it ever opens
# its debug port, which reads like a broken image rather than a bad HOME.
#
# Docker leaves HOME=/root in the environment even after we drop to uid 10001,
# and /root is not writable by runner, so this is the default case and not an
# edge case. Point it somewhere runner owns before anything can use it.
export HOME="${OASIS_HOME:-/home/runner}"
if [ "$(id -u)" = "0" ]; then
    mkdir -p "$HOME"
    chown runner:runner "$HOME" 2>/dev/null || true
fi
if [ ! -w "$HOME" ]; then
    HOME=/tmp/oasis-home
    mkdir -p "$HOME"
    export HOME
fi

if [ "$(id -u)" = "0" ]; then
    mkdir -p "$DATA_DIR"
    if ! chown -R runner:runner "$DATA_DIR" 2>/dev/null; then
        echo "[warn] could not chown $DATA_DIR - the pool may not be writable" >&2
    fi
    exec setpriv --reuid=10001 --regid=10001 --init-groups \
        python3 /app/service.py "$@"
fi

exec python3 /app/service.py "$@"
