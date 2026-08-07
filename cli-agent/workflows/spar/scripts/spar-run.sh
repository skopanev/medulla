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
# It does NOT collect the verdict: the workflow's own synthesize node writes
# verdict.md as its last act. Collecting from here meant this file had to REACH the
# container, and it did not — a live panel reported success having written nothing.
# A node that carries its own tool cannot lose it.
#
# Exit codes: 0 ok · 1 usage/preflight · 2 the run FAILED · 3 it never finished.
# Two different reactions, so two different numbers: a verdict is read, a hang is
# chased. Saying it only in stderr prose is not what an exit code is for.
set -uo pipefail

# Run history lives OUTSIDE the tree under review. It used to sit in
# .medulla/panel-runs/ inside the repo, which meant every worktree grew a directory
# and a .gitignore line it never asked for — and the panel promised not to write into
# that tree at all. One box per repo root under $HOME, named for the root and keyed by
# its full path so two worktrees of the same project never share.
# MEDULLA_PANEL_RUNS overrides it whole.
WORKFLOW="spar"               # a bare name: local .medulla/workflows/spar wins, else machine-wide
DEFAULT_TIMEOUT=2700          # 45 min: a panel is 10-20, so this is "something hung"
CONTAINER_RUNTIME="${MEDULLA_CONTAINER_RUNTIME:-docker}"

die() { echo "spar-run: $*" >&2; exit 1; }

preflight() {
    # Fail HERE, not halfway through a session, and say which piece is missing.
    command -v medulla >/dev/null 2>&1 || die "medulla is not installed (see the medulla repo)"
    case "$CONTAINER_RUNTIME" in
        docker)
            command -v docker >/dev/null 2>&1 || die "docker is not installed"
            docker info >/dev/null 2>&1 || die "the docker daemon is not responding (colima start?)"
            ;;
        apple)
            command -v container >/dev/null 2>&1 || die "Apple container CLI is not installed"
            container system status >/dev/null 2>&1 \
                || die "Apple container service is not responding (container system start)"
            ;;
        *) die "unknown container runtime: $CONTAINER_RUNTIME" ;;
    esac
    [ -d ".medulla/workflows/$WORKFLOW" ] || [ -d "$HOME/.medulla/workflows/$WORKFLOW" ] \
        || die "no spar workflow: neither ./.medulla/workflows/$WORKFLOW nor ~/.medulla/workflows/$WORKFLOW (medulla init spar)"
}

container_running() {
    case "$CONTAINER_RUNTIME" in
        docker) docker ps -q --filter 'name=^medulla-' | grep -q . ;;
        apple)  container list 2>/dev/null | grep -q 'medulla-' ;;
        *)      return 1 ;;
    esac
}

box_for() {
    # Same root -> same box, always; different worktrees of one project -> different
    # boxes. basename alone collides (three repos have a `main` worktree), the full
    # path alone is unreadable, so: name for humans, hash for identity.
    local root="$1" slug tag
    if [ -n "${MEDULLA_PANEL_RUNS:-}" ]; then
        printf '%s\n' "$MEDULLA_PANEL_RUNS"
        return 0
    fi
    slug=$(basename "$root")
    tag=$(printf '%s' "$root" | shasum 2>/dev/null | cut -c1-8)
    printf '%s\n' "$HOME/.medulla/panel-runs/${slug}-${tag}"
}

force_repo_root() {
    # Never trust the caller's CWD. Started from src/payments/, everything below would
    # be right and useless: the box lands there, the panel mounts that subtree as its
    # whole world, and the answer is confidently about a fraction of the repository.
    # The skill used to ASK for the root, which is a request, not a guarantee.
    local root
    if root=$(git rev-parse --show-toplevel 2>/dev/null) && [ -n "$root" ]; then
        [ "$root" = "$PWD" ] || echo "spar-run: running from the repo root: $root" >&2
        cd "$root" || die "cannot enter the repo root: $root"
    fi          # not a git repo: the caller's directory is the only root there is
}

cmd_start() {
    local question="${1:-}"; shift || true
    [ -n "$question" ] || die "usage: spar-run.sh start <question-file> [--mount ../repo]..."
    [ -f "$question" ] || die "question file not found: $question"
    [ -s "$question" ] || die "question file is empty: $question"
    question=$(cd "$(dirname "$question")" && pwd)/$(basename "$question")   # before cd
    force_repo_root
    preflight

    local box; box=$(box_for "$PWD")
    mkdir -p "$box" || die "cannot create $box"
    # A fixed name races: fire-and-forget is the whole design, and a second start
    # before the first medulla read its var-file would silently swap the question.
    # One id per run, for the question AND the logs. A shared run.log is worse than it
    # looks: `[ -s run.log ]` goes true on the PREVIOUS run's leftovers before this
    # medulla has truncated it, and the caller is handed a directory from an older
    # panel. Seen live — the script printed a run from eight minutes earlier.
    local id qfile log err
    id=$(date +%H%M%S)-$$
    qfile="$box/question.$id.md"; log="$box/run.$id.log"; err="$box/err.$id.log"
    cp "$question" "$qfile" || die "cannot write $qfile"

    medulla --print-run-dir "--$CONTAINER_RUNTIME" --cwd-ro --runs-folder "$box" \
        -w "$WORKFLOW" "$@" --var-file "QUESTION=$qfile" \
        >"$log" 2>"$err" &
    local pid=$!

    # Watch the PROCESS as well as the file: medulla that dies before printing — bad
    # yaml, an image that must be built, a daemon that went away — would otherwise
    # leave this loop waiting forever on a file that is never written.
    while [ ! -s "$log" ] && kill -0 "$pid" 2>/dev/null; do sleep 1; done
    if [ ! -s "$log" ]; then
        echo "spar-run: medulla failed to start:" >&2
        tail -20 "$err" >&2
        exit 2
    fi

    # The first line is the run dir — unless something warned before it, so position
    # alone is not enough. Nor is "does it exist": medulla now names the run on the
    # host BEFORE the container starts, so the directory legitimately does not exist
    # yet. What is certain is where it must be — inside the box we just named.
    # Read only COMPLETE lines: [ -s ] turns true the moment the first byte lands, and
    # a half-written path reads as nothing at all. Seen live — medulla printed the right
    # directory and the parser rejected it.
    local run="" tries=0
    while [ -z "$run" ] && [ "$tries" -lt 30 ]; do
        while IFS= read -r line; do
            case "$line" in "$box"/*) run="$line"; break ;; esac
        done < "$log"
        [ -n "$run" ] && break
        kill -0 "$pid" 2>/dev/null || break        # it died: stop waiting for a path
        sleep 1; tries=$((tries + 1))
    done
    if [ -z "$run" ]; then
        echo "spar-run: medulla printed no usable run directory:" >&2
        head -5 "$log" >&2
        exit 2
    fi

    echo "$run"
    for arg in "$@"; do
        case "$arg" in
            --mount|--mount-rw) continue ;;
            -*) continue ;;
            *) [ -d "$arg" ] && echo "spar-run: the panel sees $arg at /workspace/$(basename "$arg")" >&2 ;;
        esac
    done
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
    # NOT `[ -d "$run" ]`: medulla names the run before the container creates it, and
    # start -> wait within seconds is the documented path. Demanding the directory now
    # is the very race `start` was fixed for.

    local waited=0 gone=0
    while [ ! -f "$run/outcome.json" ]; do
        sleep 10
        waited=$((waited + 10))
        # A panel that died on its second minute should not cost forty-five. Give the
        # container a moment to appear first, then treat "no container and no outcome"
        # as death rather than patience.
        if [ "$waited" -ge 60 ] && ! container_running; then
            gone=$((gone + 1))
            if [ "$gone" -ge 2 ]; then
                echo "spar-run: no medulla container is running and no outcome was written." >&2
                echo "  run dir: $run" >&2
                [ -d "$run" ] || echo "  the run directory was never created — it died at startup" >&2
                exit 3
            fi
        else
            gone=0
        fi
        if [ "$waited" -ge "$timeout" ]; then
            echo "spar-run: still running after ${timeout}s — nothing has finished." >&2
            echo "  run dir: $run" >&2
            exit 3
        fi
    done

    # Panelist artifacts only. question.md is the input, verdict.md is this script's
    # own output, and synthesized.md is what runs before 4.34 left behind — counting
    # any of them turns four panelists into "5 artifact(s)".
    local delivered=0 f
    for f in "$run"/artifacts/*.md; do
        [ -e "$f" ] || continue
        case "$(basename "$f")" in question.md|synthesized.md|verdict.md) continue ;; esac
        delivered=$((delivered + 1))
        echo "  $f"
    done
    echo "panel finished: $delivered panelist artifact(s) in $run/artifacts/"
    grep -q '"outcome": *"succeeded"' "$run/outcome.json" 2>/dev/null || {
        echo "spar-run: the run did NOT succeed — read $run/outcome.json" >&2
        exit 2
    }
}

case "${1:-}" in
    start)    shift; cmd_start    "$@" ;;
    wait)     shift; cmd_wait     "$@" ;;
    *) die "usage: spar-run.sh start <question-file> [--mount ../repo]... | wait <run-dir>" ;;
esac
