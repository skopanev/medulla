#!/usr/bin/env bash
# Is this host idle enough to install or refresh? Answer with an EXIT CODE.
#
# It is a FILE and not a line typed before each deploy because that line was typed and
# was wrong: `docker ps --filter name=^medulla- | head; echo "(none)"` printed "(none)"
# unconditionally — the echo never depended on the filter — so a host running two panels
# read as idle and an install and a workflow refresh went out underneath them. Nothing
# broke that time. The check being broken is the defect, not the luck that followed it.
#
#   preflight-idle.sh            -> rc 0 idle · rc 1 busy (names the runs) · rc 2 cannot tell
#
# rc 2 is its own answer on purpose. "docker is not installed" and "the daemon is not
# answering" are opposite facts — the first means a native host with nothing to disturb,
# the second means a host whose state is UNKNOWN — and a check that cannot ask must not
# report idle. Same razor as the panel's quota precheck: a check that cannot run is not
# a pass.
set -uo pipefail

if ! command -v docker >/dev/null 2>&1; then
    echo "preflight: docker is not installed — no container rounds can be running here"
    exit 0
fi
if ! docker info >/dev/null 2>&1; then
    echo "preflight: the docker daemon is not answering — cannot tell what is running" >&2
    exit 2
fi

running=$(docker ps --filter 'name=^medulla-' --format '{{.Names}}\t{{.Status}}' 2>/dev/null) || {
    echo "preflight: docker ps failed — cannot tell what is running" >&2
    exit 2
}

if [ -z "$running" ]; then
    echo "preflight: idle — no medulla containers running"
    exit 0
fi

echo "preflight: BUSY — these rounds are live:" >&2
printf '%s\n' "$running" | sed 's/^/  /' >&2
echo >&2
echo "An engine install does not touch them: a container carries its own engine and" >&2
echo "self-upgrades at ITS next start. A WORKFLOW REFRESH does reach them — scripts are" >&2
echo "read from the mounted definition at the moment a node runs, so a round that has" >&2
echo "not yet reached a node will use the new copy of that node's scripts." >&2
exit 1
