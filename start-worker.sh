#!/bin/bash
# Usage: ./start-worker.sh <worker-name>
# Rootless, command-line workers. benchmarker2 is separate (start-slave.sh, root service).
set -euo pipefail

WORKER="${1:?usage: start-worker.sh <worker-name>}"

TAC="buildbot.tac"     # default for the isolated workers
EXTRA=()
# Get the sha256 hashes from https://github.com/pypy/pypy-ci/attestations
case "$WORKER" in
  aarch64)
    IMAGE="ghcr.io/pypy/buildworker_aarch64@sha256:04d6f9b5b3a44940be6a2c9e6ffc2c4aa4841e5e2b1a88a5b9e605ceb50dc928"
    PYPY="/opt/pypy2.7-v7.3.23-aarch64/bin"
    # /tmp on this worker is small; py.path's default keep-last-3 pruning
    # of usession-* translation dirs lets multi-GB dirs pile up, so keep
    # only the most recent one here.
    EXTRA=(--env PYPY_USESSION_KEEP=1) ;;
  linux-x86-64)
    IMAGE="ghcr.io/pypy/buildworker_x86_64@sha256:4b502665cce7a324fe07eb22f755e4a0040affca8d04fb8085828fab99c24b0d"
    PYPY="/opt/pypy2.7-v7.3.23-linux64/bin" ;;
  benchmarker2-32)
    # colocated with the master, hence --network=host
    IMAGE="ghcr.io/pypy/buildworker_i686@sha256:b38c4b362539b5a1a4825c9d4415a4939dd9aece01e164cf44c7792535738451"
    TAC="benchmarker2-32.tac"
    PYPY="/opt/pypy2.7-v7.3.23-linux32/bin"
    EXTRA=(--platform=linux/386 --network=host) ;;
  *) echo "unknown worker: $WORKER (expected aarch64, linux-x86-64, or benchmarker2-32)" >&2; exit 1 ;;
esac

SLAVE_DIR="$HOME/buildbot/slave"
[[ -f "$SLAVE_DIR/$TAC" ]] || { echo "no $SLAVE_DIR/$TAC" >&2; exit 1; }
[[ -f "$HOME/buildbot/requirements.txt" ]] || { echo "no requirements.txt" >&2; exit 1; }

# Record when this worker was (re)started, shown on the worker's status page.
HOST_INFO="$SLAVE_DIR/info/host"
TIMESTAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
mkdir -p "$(dirname "$HOST_INFO")"
if [[ -f "$HOST_INFO" ]] && grep -q '^last-started:' "$HOST_INFO"; then
    sed -i "s/^last-started:.*/last-started: $TIMESTAMP/" "$HOST_INFO"
else
    echo "last-started: $TIMESTAMP" >> "$HOST_INFO"
fi

TMP="/tmp/$WORKER"
mkdir -p "$TMP"; chmod 1777 "$TMP"
podman rm -f "$WORKER" 2>/dev/null || true

exec podman run --rm -d --init\
    --name "$WORKER" \
    --userns=keep-id \
    --volume "$HOME/buildbot:/buildbot" \
    --volume "$TMP:/tmp" \
    --workdir /buildbot/slave \
    --env HGDEMANDIMPORT=disable \
    --env TERM=xterm \
    --env PYPY_MAKE_PORTABLE=1 \
    "${EXTRA[@]}" \
    "$IMAGE" \
    /bin/bash -c "
        set -euo pipefail
        PATH=\$PATH:$PYPY
        if [ -x /tmp/venv/bin/twistd ]; then
            . /tmp/venv/bin/activate
        else
            pypy -m virtualenv /tmp/venv
            . /tmp/venv/bin/activate
            pip install --no-cache-dir -r /buildbot/requirements.txt zstandard
        fi
        cd /buildbot/slave
        exec /tmp/venv/bin/twistd --nodaemon --pidfile= --python=$TAC
    "
