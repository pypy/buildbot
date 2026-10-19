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
    IMAGE="ghcr.io/pypy/buildworker_aarch64@sha256:3aacb5c98d35da766cf25f25defc3edad0417fb71b42e3270b9420802d83b310"
    PYPY="/opt/pypy2.7-v8.0.0-aarch64/bin"
    # /tmp on this worker is small; py.path's default keep-last-3 pruning
    # of usession-* translation dirs lets multi-GB dirs pile up, so keep
    # only the most recent one here.
    EXTRA=(--env PYPY_USESSION_KEEP=1) ;;
  linux-x86-64)
    IMAGE="ghcr.io/pypy/buildworker_x86_64@sha256:7d141d0a56ab277052e9b5e82459301f2b7ad13670e068a9e87b6ffbb5d26153"
    PYPY="/opt/pypy2.7-v8.0.0-linux64/bin" ;;
  benchmarker2-32)
    # colocated with the master, hence --network=host
    IMAGE="ghcr.io/pypy/buildworker_i686@sha256:16279ad646835272bc8dd688ffc50e475b81610bab41cc3189e0f87a35d69082"
    TAC="benchmarker2-32.tac"
    PYPY="/opt/pypy2.7-v8.0.0-linux32/bin"
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
