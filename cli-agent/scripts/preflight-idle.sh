#!/usr/bin/env bash
# Is this host idle enough to install or refresh? Answer with an EXIT CODE.
#
# It is a FILE and not a line typed before each deploy because that line was typed and
# was wrong: `docker ps --filter name=^medulla- | head; echo "(none)"` printed "(none)"
# unconditionally — the echo never depended on the filter — so a host running two panels
# read as idle and an install and a workflow refresh went out underneath them. Nothing
# broke that time. The check being broken is the defect, not the luck that followed it.
#
#   preflight-idle.sh [refresh]  -> rc 0 idle · rc 1 busy (names the runs) · rc 2 cannot tell
#   preflight-idle.sh install    -> rc 0 safe  · rc 1 a host engine is running
#
# TWO QUESTIONS, NOT ONE. The first version answered only "are containers up", refused an
# INSTALL on that basis, and blocked a release the owner was waiting for — while the
# rounds it was protecting could not have been touched by it. A guard that stops safe work
# gets worked around by hand, and then it guards nothing.
#
#   install  replaces ~/.medulla/engine/venv and the bin symlink. A running container
#            carries its OWN engine and self-upgrades at ITS next start, so live rounds
#            are not reachable. What IS at risk is a medulla process running on the host
#            right now, and a new invocation during the few seconds the venv is being
#            replaced. So: ask about host processes.
#   refresh  rewrites the workflow definition that containers have MOUNTED. A node that
#            has not run yet will read the new scripts mid-round. So: ask about containers.
#
# rc 2 is its own answer on purpose. "docker is not installed" and "the daemon is not
# answering" are opposite facts — the first means a native host with nothing to disturb,
# the second means a host whose state is UNKNOWN — and a check that cannot ask must not
# report idle. Same razor as the panel's quota precheck: a check that cannot run is not
# a pass.
set -uo pipefail

MODE="${1:-refresh}"

if [ "$MODE" = install ]; then
    # THE INSTALLED PACKAGE PATH, resolved — not the word "medulla", not a guessed one.
    # Two wrong attempts are worth recording, because each FAILED TOWARD SAYING YES:
    #
    #   `pgrep -f medulla` matched the lane launcher's argv — a launch.json path, a
    #   --var EQUILL_BRIDGE=/tmp/medulla-bridge/... — and reported engines that were not.
    #
    #   "$HOME/.medulla/engine/venv/bin" matched nothing, because the engine on this host
    #   lives under /Volumes/hdd/.medulla and the running processes are
    #   site-packages/medulla/scripts/docker.py, not bin/medulla. It answered "safe to
    #   install" with three live engine processes on the host. A guard that errs toward
    #   yes is worse than no guard: I believed it and nearly replaced the venv under them.
    #
    # So resolve where the entry point actually points, and look for that tree in argv.
    # FULLY resolved: ~/.medulla is itself a symlink to /Volumes/hdd/.medulla here, so a
    # one-level readlink gave /Users/... while every running process shows /Volumes/...
    # — the same venv under two names, and the check matched neither. `cd && pwd` resolves
    # the whole chain.
    engine_bin=$(cd "$(dirname "$HOME/.local/bin/medulla")" 2>/dev/null && pwd -P)/medulla
    engine_root=$(cd "$(dirname "$(readlink "$engine_bin" 2>/dev/null || echo "$engine_bin")")/.." 2>/dev/null && pwd -P)
    if [ -z "$engine_root" ]; then
        echo "preflight: cannot resolve the installed engine path — cannot tell" >&2
        exit 2
    fi
    # PID and the engine script only. A lane invocation carries hundreds of --var
    # arguments, and printing argv buried the answer under one unreadable line.
    # PID and the engine script only. A lane invocation carries hundreds of --var
    # arguments across several LINES, so printing argv buried the answer. Keep the lines
    # that start with a pid, drop the rest of each.
    running=$(pgrep -fl "$engine_root" 2>/dev/null | grep -v "preflight-idle" \
              | sed -nE "s#^([0-9]+).*/site-packages/medulla/([^ ]*).*#  \1  medulla/\2#p" \
              | head -8)
    if [ -n "$running" ]; then
        echo "preflight: a medulla engine is running on the host — install would replace" >&2
        echo "the venv underneath it:" >&2
        printf '%s\n' "$running" >&2
        exit 1
    fi
    echo "preflight: safe to install — no medulla engine running on this host"
    echo "  (live containers carry their own engine and self-upgrade at their next start)"
    exit 0
fi

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
