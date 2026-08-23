#!/usr/bin/env bash
# Start a spar panel, or wait for one. The whole launch contract, as code.
#
# It is a FILE and not a snippet in SKILL.md because the repo's own AGENTS.md says
# LLMs cannot be trusted to reproduce exact commands — and the snippet it replaced had
# a heredoc, a background job, a poll loop and a $PID in it. Every one of those is a
# way to get it subtly wrong, and a wrong launch looks exactly like a dead panel.
#
#   spar-run.sh start <question-file> [--mount ../repo]...   -> prints the run dir
#   spar-run.sh wait  <run-dir> [--timeout SECONDS]          -> waits, then reports
#
# Exit codes: 0 ok · 1 usage/preflight · 2 the panel failed or never finished.
set -uo pipefail

BOX_NAME=".medulla/panel-runs"
WORKFLOW=".medulla/workflows/spar"
DEFAULT_TIMEOUT=2700          # 45 min: a panel is 10-20, so this is "something hung"

die() { echo "spar-run: $*" >&2; exit 1; }

preflight() {
    # Fail HERE, not halfway through a session, and say which piece is missing.
    command -v medulla >/dev/null 2>&1 || die "medulla is not installed (see the medulla repo)"
    command -v docker  >/dev/null 2>&1 || die "docker is not installed — the panel runs in a container"
    docker info >/dev/null 2>&1        || die "the docker daemon is not responding (colima start?)"
    [ -d "$WORKFLOW" ] || [ -d "$HOME/$WORKFLOW" ] \
        || die "no spar workflow: neither ./$WORKFLOW nor ~/$WORKFLOW"
}

ignore_the_box() {
    # The box lives in the repo, so it must not reach the index of the very tree the
    # panel promised not to touch. Say so out loud: a silent write into someone's
    # .gitignore is the kind of help nobody asked for.
    local gi=".gitignore"
    git rev-parse --git-dir >/dev/null 2>&1 || return 0      # not a repo: nothing to ignore
    if ! grep -q '^\.medulla/panel-runs/' "$gi" 2>/dev/null; then
        if printf '%s\n' "$BOX_NAME/" >> "$gi" 2>/dev/null; then
            echo "spar-run: added $BOX_NAME/ to .gitignore" >&2
        else
            echo "spar-run: WARNING cannot write .gitignore — $BOX_NAME/ will show up in git status" >&2
        fi
    fi
}

cmd_start() {
    local question="${1:-}"; shift || true
    [ -n "$question" ] || die "usage: spar-run.sh start <question-file> [--mount ../repo]..."
    [ -f "$question" ] || die "question file not found: $question"
    [ -s "$question" ] || die "question file is empty: $question"
    preflight
    ignore_the_box

    local box="$PWD/$BOX_NAME"
    mkdir -p "$box" || die "cannot create $box"
    cp "$question" "$box/question.md"

    medulla --print-run-dir --docker --cwd-ro --runs-folder "$box" \
        -w "$WORKFLOW" "$@" --var-file "QUESTION=$box/question.md" \
        >"$box/run.log" 2>"$box/err.log" &
    local pid=$!

    # Watch the PROCESS as well as the file: medulla that dies before printing — bad
    # yaml, an image that must be built, a daemon that went away — would otherwise
    # leave this loop waiting forever on a file that is never written.
    while [ ! -s "$box/run.log" ] && kill -0 "$pid" 2>/dev/null; do sleep 1; done
    if [ ! -s "$box/run.log" ]; then
        echo "spar-run: medulla failed to start:" >&2
        tail -20 "$box/err.log" >&2
        exit 2
    fi

    # The first line is the run dir — unless something warned before it, so position
    # alone is not enough. Nor is "does it exist": medulla now names the run on the
    # host BEFORE the container starts, so the directory legitimately does not exist
    # yet. What is certain is where it must be — inside the box we just named.
    local run=""
    while IFS= read -r line; do
        case "$line" in "$box"/*) run="$line"; break ;; esac
    done < "$box/run.log"
    if [ -z "$run" ]; then
        echo "spar-run: medulla printed no usable run directory:" >&2
        head -5 "$box/run.log" >&2
        exit 2
    fi

    echo "$run"
    echo "spar-run: panel started (pid $pid). Wait for it with:" >&2
    echo "  spar-run.sh wait '$run'" >&2
}

cmd_wait() {
    local run="${1:-}"; shift || true
    local timeout=$DEFAULT_TIMEOUT
    while [ $# -gt 0 ]; do
        case "$1" in
            --timeout) timeout="${2:-$DEFAULT_TIMEOUT}"; shift 2 ;;
            *) die "unknown option: $1" ;;
        esac
    done
    [ -n "$run" ] || die "usage: spar-run.sh wait <run-dir> [--timeout SECONDS]"
    [ -d "$run" ] || die "not a run directory: $run"

    local waited=0
    while [ ! -f "$run/outcome.json" ]; do
        sleep 10
        waited=$((waited + 10))
        if [ "$waited" -ge "$timeout" ]; then
            # Not a failure of the panel — a failure to finish. Say which, because the
            # two need different reactions: one is a verdict, the other is a hang.
            echo "spar-run: still running after ${timeout}s — nothing has finished." >&2
            echo "  run dir: $run" >&2
            echo "  live containers: $(docker ps --format '{{.Names}}' | grep -c '^medulla-' || true)" >&2
            exit 2
        fi
    done

    local delivered
    delivered=$(ls "$run"/artifacts/*.md 2>/dev/null | grep -vc 'question\.md$' || true)
    echo "panel finished: $delivered artifact(s) in $run/artifacts/"
    ls "$run"/artifacts/*.md 2>/dev/null | grep -v 'question\.md$' | sed 's|^|  |'
    grep -q '"outcome": *"succeeded"' "$run/outcome.json" 2>/dev/null || {
        echo "spar-run: the run did NOT succeed — read $run/outcome.json" >&2
        exit 2
    }
}

case "${1:-}" in
    start) shift; cmd_start "$@" ;;
    wait)  shift; cmd_wait  "$@" ;;
    *) die "usage: spar-run.sh start <question-file> [--mount ../repo]... | wait <run-dir>" ;;
esac
