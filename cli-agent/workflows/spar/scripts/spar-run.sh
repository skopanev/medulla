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
FALLBACK_TIMEOUT=3900         # only when the workflow's own deadline cannot be read

# How long to wait is not an independent opinion — it is the workflow's deadline plus
# room to conclude. They had drifted apart: the wait gave up at 2700s while the run was
# entitled to 3600, so a lane saw "timed out, no verdict" fifteen minutes before the
# engine would have stopped anything. Measured live: container up 56 minutes, wait
# expired at 45, three of four panelists delivered and the round had already met
# min_success — a completed run whose last worker was still inside its budget, holding
# a lane slot with no work left in it. Deriving the number keeps them together when
# either changes.
default_timeout() {
    local wf grace=300
    for wf in ".medulla/workflows/$WORKFLOW/workflow.yaml" \
              "$HOME/.medulla/workflows/$WORKFLOW/workflow.yaml"; do
        [ -f "$wf" ] || continue
        local t
        t=$(sed -n 's/^timeout:[[:space:]]*\([0-9][0-9]*\).*/\1/p' "$wf" | head -1)
        case "$t" in ''|*[!0-9]*) continue ;; esac
        echo $((t + grace)); return 0
    done
    echo "$FALLBACK_TIMEOUT"
}

die() { echo "spar-run: $*" >&2; exit 1; }

# How many panel runs are still going. Containers when we launched into one,
# otherwise medulla processes on this host.
alive_count() {
    # "Is MY round alive", not "is any panel alive". Filtering on `^medulla-` answered
    # for every panel on the machine, so a wait sat quietly through its own round's
    # death whenever a stranger's round was up — measured by a lane whose wait hung on
    # a round that had already died. Containers are named after their run directory
    # (4.72.0+), so the question can finally be asked about one run.
    local run="${1:-}"
    if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
        if [ -n "$run" ]; then
            docker ps -q --filter "name=^medulla-$(basename "$run")\$" 2>/dev/null | wc -l
        else
            docker ps -q --filter 'name=^medulla-' 2>/dev/null | wc -l
        fi
    else
        pgrep -f 'medulla .*-w ' 2>/dev/null | wc -l
    fi
}

preflight() {
    # Fail HERE, not halfway through a session, and say which piece is missing.
    command -v medulla >/dev/null 2>&1 || die "medulla is not installed (see the medulla repo)"
    # Docker is PREFERRED, not required. It gives the panel a clean box and a
    # read-only tree. When the container runtime is down, a panel that refuses to
    # start is worse than a smaller panel that does: a whole container fault once
    # took spar with it, though nothing about the workflow needs a container.
    DOCKER_OK=yes
    if ! command -v docker >/dev/null 2>&1; then
        DOCKER_OK=no; DOCKER_WHY="docker is not installed"
    elif ! docker info >/dev/null 2>&1; then
        DOCKER_OK=no; DOCKER_WHY="the docker daemon is not responding"
    elif ! docker images >/dev/null 2>&1; then
        # The daemon answers but its image store does not — a live failure mode
        # (a corrupted content store answers `docker ps` and fails every run).
        DOCKER_OK=no; DOCKER_WHY="the docker image store is unreadable"
    fi
    [ -d ".medulla/workflows/$WORKFLOW" ] || [ -d "$HOME/.medulla/workflows/$WORKFLOW" ] \
        || die "no spar workflow: neither ./.medulla/workflows/$WORKFLOW nor ~/.medulla/workflows/$WORKFLOW (medulla init spar)"
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

in_tmp() {
    # Canonical prefixes only: on macOS /tmp and $TMPDIR are symlinks into /private,
    # and a caller who passes /tmp/q.md must not be judged by the string they typed.
    case "$1" in
        /tmp/*|/private/tmp/*|/var/folders/*|/private/var/folders/*) return 0 ;;
    esac
    [ -n "${TMPDIR:-}" ] || return 1
    local t; t=$(cd "$TMPDIR" 2>/dev/null && pwd -P) || return 1
    case "$1" in "$t"/*) return 0 ;; esac
    return 1
}

cmd_start() {
    local question="${1:-}"; shift || true
    [ -n "$question" ] || die "usage: spar-run.sh start <question-file> [--mount ../repo]..."
    [ -f "$question" ] || die "question file not found: $question"
    [ -s "$question" ] || die "question file is empty: $question"
    question=$(cd "$(dirname "$question")" && pwd -P)/$(basename "$question")   # before cd

    # The question is COPIED into the run's box below, so the caller's file is dead
    # weight one second after this returns — and left in a repo it is litter that
    # outlives the round by months. One workspace collected 71 stray panel-question,
    # panel-handout and BRIEF files, 292 MB, in its root before anyone noticed. Asking
    # for /tmp in the skill is a request; this is the guarantee. A human with a brief
    # they wrote by hand and want to keep passes --allow-outside-tmp.
    local allow_outside=no arg n=$#
    while [ "$n" -gt 0 ]; do             # rotate each argument exactly once
        arg=$1; shift; n=$((n - 1))
        case "$arg" in
            --allow-outside-tmp) allow_outside=yes ;;
            *) set -- "$@" "$arg" ;;
        esac
    done
    if [ "$allow_outside" = no ] && ! in_tmp "$question"; then
        echo "spar-run: the question file lives outside \$TMPDIR:" >&2
        echo "  $question" >&2
        echo "spar-run: start COPIES the question into the run's own box, so this file" >&2
        echo "spar-run: is useless the moment the panel starts — and it stays behind in" >&2
        echo "spar-run: the tree forever. Write the brief (and any diff or handout you" >&2
        echo "spar-run: generate for the round) under a mktemp path instead:" >&2
        echo "spar-run:   q=\$(mktemp -t spar-q).md && ... && medulla launch spar start \"\$q\"" >&2
        die "refusing to litter; pass --allow-outside-tmp to override"
    fi

    force_repo_root
    preflight

    local box; box=$(box_for "$PWD")
    mkdir -p "$box" || die "cannot create $box"
    # The engine prints physical paths. Resolve after mkdir so a symlinked run
    # root (or a relative override) is compared using the same spelling below.
    box=$(cd "$box" && pwd -P) || die "cannot resolve $box"
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

    # Container mode also mounts the tree read-only; native mode cannot, so the
    # panel is trusted with the working tree it reviews. Say so rather than let it
    # be discovered.
    if [ "$DOCKER_OK" = yes ]; then
        set -- --docker --cwd-ro "$@"
    else
        echo "spar-run: $DOCKER_WHY — running the panel NATIVELY on the host." >&2
        echo "spar-run: reduced panel — harnesses without host credentials sit out;" >&2
        echo "spar-run: expect roughly 3 of 5 panelists. min_success is 3, so quorum" >&2
        echo "spar-run: is still reachable, but the round is thinner than in a container." >&2
        echo "spar-run: the tree is NOT mounted read-only in this mode." >&2
    fi
    # NAME THE LANE. The run directory, and therefore the container, is named from
    # this; without it both are a timestamp and eight random hex, and an owner asking
    # "how many lanes are up and on what" gets a list that answers neither. The box
    # already carries the worktree or ticket this round is about, so use it. A caller
    # that set its own MEDULLA_RUN_ID keeps it — this is a default, not a policy. The
    # pid keeps two rounds started in the same second in the same box from colliding
    # on a directory name, which fire-and-forget makes possible.
    : "${MEDULLA_RUN_ID:="${box##*/}-$$"}"
    export MEDULLA_RUN_ID

    # OUTLIVE THE SESSION THAT STARTED IT. `cmd &` leaves the child in this shell's
    # process group, so when the caller's session ends the kernel sends SIGHUP to the
    # group and the panel dies mid-round — after twenty minutes of paid models. Twice
    # this shows as a round that plainly FINISHED: journal terminal, verdict.json and
    # verdict.md on disk, and no outcome.json, because the engine writes that last and
    # never got there. `wait` then reported "no container and no outcome" about a
    # review that was complete, which is the worst direction to be wrong in.
    # nohup, not setsid: setsid is not on macOS. stdout and stderr are already
    # redirected, so nohup writes no nohup.out.
    nohup medulla --print-run-dir --runs-folder "$box" \
        -w "$WORKFLOW" "$@" --var-file "QUESTION=$qfile" \
        >"$log" 2>"$err" &
    local pid=$!
    disown "$pid" 2>/dev/null || true

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

panel_state() {
    # The reader lives beside this script: python is guaranteed wherever medulla runs
    # (medulla IS python), and jq is one more thing that can be missing on a host.
    python3 "$(dirname "$0")/panel_state.py" "$1" 2>/dev/null
}

cmd_wait() {
    local run="${1:-}"; shift || true
    local timeout; timeout=$(default_timeout)
    while [ $# -gt 0 ]; do
        case "$1" in
            --timeout) timeout="${2:-$(default_timeout)}"; shift 2 ;;
            *) die "unknown option: $1" ;;
        esac
    done
    [ -n "$run" ] || die "usage: spar-run.sh wait <run-dir> [--timeout SECONDS]"
    # NOT `[ -d "$run" ]`: medulla names the run before the container creates it, and
    # start -> wait within seconds is the documented path. Demanding the directory now
    # is the very race `start` was fixed for.

    # A ROUND CAN FINISH WITHOUT BEING MARKED FINISHED. outcome.json is written last,
    # so an engine killed after the terminal transition leaves a COMPLETE review with
    # no marker: journal terminal, verdict.json and verdict.md on disk. Read the
    # JOURNAL, never the presence of a verdict file — a verdict exists long before the
    # round ends, and treating it as completion would hide real failures.
    journal_terminal() {
        python3 "$(dirname "$0")/panel_state.py" --terminal "$1" 2>/dev/null
    }

    local waited=0 gone=0 terminal=""
    while [ ! -f "$run/outcome.json" ]; do
        terminal=$(journal_terminal "$run") || terminal=""
        if [ -n "$terminal" ]; then
            echo "spar-run: the round FINISHED but was never marked finished ($terminal)." >&2
            echo "  The journal reached a terminal state and outcome.json is absent," >&2
            echo "  which means the engine was killed after the last node completed." >&2
            echo "  What follows is the verdict of a completed round, not a partial one." >&2
            break
        fi
        sleep 10
        waited=$((waited + 10))
        # A panel that died on its second minute should not cost forty-five. Give the
        # container a moment to appear first, then treat "no container and no outcome"
        # as death rather than patience.
        # "Is it still alive" is asked differently per mode: a container by name, a
        # native run by its process. Asking docker in native mode reports every
        # healthy panel as dead sixty seconds in.
        # The grace before "no container means dead" is a real container start, which
        # is seconds — but a build makes it minutes, so it stays generous by default.
        # Overridable so a test can reach this branch without sleeping through it.
        if [ "$waited" -ge "${SPAR_STARTUP_GRACE_S:-60}" ] && [ "$(alive_count "$run")" -eq 0 ]; then
            gone=$((gone + 1))
            if [ "$gone" -ge 2 ]; then
                echo "spar-run: no medulla container is running and no outcome was written." >&2
                echo "  run dir: $run" >&2
                if [ ! -d "$run" ]; then
                    echo "  the run directory was never created — it died at startup" >&2
                    # AND THE REASON IS ONE FILE AWAY. `start` is fire-and-forget: it
                    # prints the run directory and returns 0 the moment medulla names
                    # it, which is BEFORE the container has done anything. When the
                    # engine then dies — a workflow file that was not there, an image
                    # that will not build — there is no run directory, no journal and
                    # no outcome to read, and the caller was told the panel started.
                    # Reported live: medulla exited on FileNotFoundError for its own
                    # workflow.yaml and the lane had nothing to go on but silence.
                    # The launcher's stderr caught it and nobody was looking there.
                    for _l in "$(dirname "$run")"/run.*.log; do
                        [ -f "$_l" ] || continue
                        grep -qF "$run" "$_l" 2>/dev/null || continue
                        _e="$(dirname "$_l")/err.${_l##*/run.}"
                        [ -s "$_e" ] || continue
                        echo "  the launcher's error log ($_e) says:" >&2
                        tail -20 "$_e" | sed 's/^/    /' >&2
                    done
                fi
                exit 3
            fi
        else
            gone=0
        fi
        if [ "$waited" -ge "$timeout" ]; then
            local state summary
            state=$(panel_state "$run")
            summary=$(printf '%s' "$state" | sed -n 's/^@@ //p')
            if [ -n "$summary" ]; then
                set -- $summary
                echo "spar-run: still running after ${timeout}s — $1/$2 delivered${3:+, waiting on ${*:3}}." >&2
                printf '%s\n' "$state" | grep -v '^@@' >&2
            else
                echo "spar-run: still running after ${timeout}s — nothing recorded yet." >&2
            fi
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
    # A FINISHED FAILURE IS STILL A FAILURE. With no outcome.json there is nothing to
    # grep, and grepping a missing file would have called every unmarked round a
    # failure — including the completed ones this branch exists to rescue. The journal
    # already said which terminal it reached; use that, and say where it came from.
    # `[ x ] && { ... }` here would BE the function's exit status when the test is
    # false — a successful round would return 1 for having nothing to report.
    if [ ! -f "$run/outcome.json" ]; then
        if [ "$terminal" = "__exit_fail__" ]; then
            echo "spar-run: the run FAILED (journal: __exit_fail__; outcome.json was never written)" >&2
            exit 2
        fi
    else
        grep -q '"outcome": *"succeeded"' "$run/outcome.json" 2>/dev/null || {
            echo "spar-run: the run did NOT succeed — read $run/outcome.json" >&2
            exit 2
        }
    fi
}

case "${1:-}" in
    start)    shift; cmd_start    "$@" ;;
    wait)     shift; cmd_wait     "$@" ;;
    *) die "usage: spar-run.sh start <question-file> [--mount ../repo]... | wait <run-dir>" ;;
esac
