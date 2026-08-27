#!/bin/bash
# Usage: ./restart-worker-weekly.sh [worker-name]
# Meant to run from cron on bencher4, once a week, to work around a leak/
# hang in the linux-x86-64 worker (runs there in podman, see start-worker.sh).
#
# Status pages on buildbot.pypy.org are plain unauthenticated GET; the
# pause/unpause POSTs are covered by pauseSlave=True in the Authz config
# (bot2/pypybuildbot/master.py), which does not require login either.
#
# Sequence: pause (stop new builds being assigned) -> poll until idle
# (safely wait out anything already in flight) -> kill+restart the local
# worker -> unpause (resume normal scheduling).
set -euo pipefail

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    echo "Usage: $(basename "$0") [worker-name] [builder-name]"
    echo
    echo "  worker-name   buildbot slave/worker name known to the master, used in the"
    echo "                buildslaves status/pause URLs (default: bencher4)"
    echo "  builder-name  arg to start-worker.sh, picks the podman image/config;"
    echo "                NOT the same name as worker-name (default: linux-x86-64)"
    exit 0
fi

# WORKER is the buildbot slave/worker name known to the master (used in
# the buildslaves status/pause URLs). BUILDER is the start-worker.sh arg,
# which picks the podman image/config - they are NOT the same name.
WORKER="${1:-bencher4}"
BUILDER="${2:-linux-x86-64}"
BASE_URL="https://buildbot.pypy.org"
POLL_INTERVAL=30       # seconds between idle checks
MAX_WAIT=$((3 * 3600))  # give up waiting for idle after 3 hours

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCK_FILE="/tmp/restart-worker-weekly-$WORKER.lock"

exec 9>"$LOCK_FILE"
flock -n 9 || { echo "another restart-worker-weekly run for $WORKER is still active, exiting" >&2; exit 1; }

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }

is_idle() {
    curl -sf "$BASE_URL/buildslaves/$WORKER" | grep -q 'No current builds'
}

log "pausing $WORKER"
curl -sf -X POST "$BASE_URL/buildslaves/$WORKER/pause" -o /dev/null

log "waiting for $WORKER to go idle (checking every ${POLL_INTERVAL}s, giving up after ${MAX_WAIT}s)"
waited=0
while ! is_idle; do
    if (( waited >= MAX_WAIT )); then
        log "$WORKER did not go idle within ${MAX_WAIT}s, unpausing and aborting restart"
        curl -sf -X POST "$BASE_URL/buildslaves/$WORKER/unpause" -o /dev/null
        exit 1
    fi
    sleep "$POLL_INTERVAL"
    waited=$((waited + POLL_INTERVAL))
done

log "$WORKER is idle, restarting $BUILDER"
"$SCRIPT_DIR/start-worker.sh" "$BUILDER"

log "unpausing $WORKER"
curl -sf -X POST "$BASE_URL/buildslaves/$WORKER/unpause" -o /dev/null

log "done"
