#!/bin/bash
# Usage: ./start-worker.sh <worker-name>
# Rootless, command-line workers. benchmarker2 is separate (start-slave.sh, root service).
set -euo pipefail

WORKER="${1:?usage: start-worker.sh <worker-name>}"
PYPY="/opt/pypy2.7-v7.3.22-linux64/bin"   # inside the container

TAC="buildbot.tac"     # default for the isolated workers
EXTRA=()
# Get the sha256 hashes from https://github.com/pypy/pypy-ci/attestations
case "$WORKER" in
  aarch64)
    IMAGE="ghcr.io/pypy/buildworker_aarch64@sha256:85089da46253cf4a27da086fe9d00a7f8bb3551cf898ca71e0a6ddd1414c6abd" ;;
  linux-x86-64)
    IMAGE="ghcr.io/pypy/buildworker_x86_64@sha256:c9f502a46d9438a11b2cac9a27d10d9449bdbcfde11cb46b47fd569aa7b476ba" ;;
  benchmarker2-32)
    # colocated with the master, hence --network=host
    IMAGE="ghcr.io/pypy/buildworker_i686@sha256:20c7ca7528686a207ed9d5952a52c5104d14455da36abf1eea84ce816988f07f"
    TAC="benchmarker2-32.tac"
    EXTRA=(--platform=linux/386 --network=host) ;;
  *) echo "unknown worker: $WORKER" >&2; exit 1 ;;
esac

SLAVE_DIR="$HOME/buildbot/slave"
[[ -f "$SLAVE_DIR/$TAC" ]] || { echo "no $SLAVE_DIR/$TAC" >&2; exit 1; }
[[ -f "$HOME/buildbot/requirements.txt" ]] || { echo "no requirements.txt" >&2; exit 1; }

TMP="/tmp/$WORKER"
mkdir -p "$TMP"; chmod 1777 "$TMP"
podman rm -f "$WORKER" 2>/dev/null || true

exec podman run --rm -d \
    --name "$WORKER" \
    --userns=keep-id \
    --volume "$HOME/buildbot:/buildbot" \
    --volume "$TMP:/tmp" \
    --workdir /buildbot/slave \
    --env HGDEMANDIMPORT=disable \
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
