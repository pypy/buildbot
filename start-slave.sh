#!/bin/bash
# Start the PyPy buildslave inside a pinned-CPU podman container.

set -euo pipefail

# Configuration
IMAGE="ghcr.io/pypy/buildworker_x86_64@sha256:c9f502a46d9438a11b2cac9a27d10d9449bdbcfde11cb46b47fd569aa7b476ba"
PYPY="/opt/pypy2.7-v7.3.22-linux64/bin"  # inside the container
SLAVE_DIR="$HOME/buildbot/slave"
REQUIREMENTS="$HOME/buildbot/requirements.txt"
CONTAINER_NAME="pypy-buildslave"
HOSTNAME="benchmarker2"
CPUS="3,4,5"

# Pre-flight checks
if [[ ! -d "$SLAVE_DIR" ]]; then
    echo "ERROR: slave directory not found: $SLAVE_DIR" >&2
    exit 1
fi
if [[ ! -f "$REQUIREMENTS" ]]; then
    echo "ERROR: requirements file not found: $REQUIREMENTS" >&2
    exit 1
fi

# Remove any stale container with the same name
podman rm -f "$CONTAINER_NAME" 2>/dev/null || true

mkdir -p /tmp/buildworker
chmod 1777 /tmp/buildworker

# Run the slave
exec podman run \
    --rm -it\
    --name "$CONTAINER_NAME" \
    --hostname "$HOSTNAME" \
    --cpuset-cpus="$CPUS" \
    --user 1001:1001 \
    --volume "/home/pypy-worker/buildbot:/buildbot" \
    --volume /tmp/buildworker:/tmp \
    --workdir /buildbot/slave \
    --env HGDEMANDIMPORT=disable \
    "$IMAGE" \
    /bin/bash -c "
        set -euo pipefail

        PATH=$PATH:$PYPY

        if [ -x /tmp/venv/bin/twistd ]; then
            . /tmp/venv/bin/activate
        else
            pypy -m virtualenv /tmp/venv
            . /tmp/venv/bin/activate
            pip install --no-cache-dir -r /buildbot/requirements.txt zstandard
        fi

        # Start buildslave in foreground via twistd directly
        # buildbot 0.8.8 does not have the --nodaemon option
        cd /buildbot/slave
        exec /tmp/venv/bin/twistd --nodaemon --pidfile= --python=buildbot.tac
    "
