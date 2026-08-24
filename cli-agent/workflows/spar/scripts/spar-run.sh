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

die() { echo "spar-run: $*" >&2; exit 1; }

preflight() {
    # Fail HERE, not halfway through a session, and say which piece is missing.
    command -v medulla >/dev/null 2>&1 || die "medulla is not installed (see the medulla repo)"
    command -v docker  >/dev/null 2>&1 || die "docker is not installed — the panel runs in a container"
    docker info >/dev/null 2>&1        || die "the docker daemon is not responding (colima start?)"
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

    medulla --print-run-dir --docker --cwd-ro --runs-folder "$box" \
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
        if [ "$waited" -ge 60 ] && [ "$(docker ps -q --filter 'name=^medulla-' | wc -l)" -eq 0 ]; then
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

cmd_findings() {
    local run="${1:-}"
    [ -n "$run" ] || die "usage: spar-run.sh findings <run-dir>"
    [ -d "$run/artifacts" ] || die "no artifacts in: $run"

    # Cut the VERDICT and FINDINGS sections out mechanically, into markdown. Asking a
    # summariser not to summarise is asking water to be dry: a model reading five files
    # and retelling them WILL drop the lone finding, which is the one the panel was
    # convened for. awk has no opinions. The prose above each list stays where it is —
    # read it too, but read it knowing nothing was lost on the way here.
    local out="$run/verdict.md" body="$run/.verdict.body"
    : > "$body"

    local f slug n total=0 go=0 nogo=0 insuf=0 nover=0 word
    {
        printf '## Verdicts\n\n'
        for f in "$run"/artifacts/*.md; do
            case "$(basename "$f")" in question.md|synthesized.md|verdict.md) continue ;; esac
            slug=$(basename "$f" .md)
            word=$(awk '/^## VERDICT/{flag=1; next} /^## /{flag=0}
                        flag && NF {print; exit}' "$f")
            case "$word" in
                GO*)           go=$((go + 1)) ;;
                NO-GO*)        nogo=$((nogo + 1)) ;;
                INSUFFICIENT*) insuf=$((insuf + 1)) ;;
                *)             nover=$((nover + 1)); word="_no VERDICT section_" ;;
            esac
            printf -- '- **%s** — %s\n' "$slug" "$word"
        done
        printf '\n'
    } >> "$body"

    # ONE list, not a section per panelist. Five headings with counts made the reader
    # compare panelists, which is not the job — the job is to see every defect. Who
    # said it rides at the FRONT of its own line, so nothing is anonymous and nothing
    # is buried under a name. A panelist with no findings gets one line at the end
    # rather than an empty section: "found nothing" and "never answered" must stay
    # distinguishable, and an empty heading says neither.
    # ONE list, sorted by the severity the FINDER gave it. Not a section per panelist:
    # that made the reader compare panelists, which is not the job — the job is to see
    # what is worst first. Who said it rides at the FRONT of its own line, so nothing is
    # anonymous and nothing is buried under a name.
    local quiet="" raw="$run/.verdict.raw"
    : > "$raw"
    for f in "$run"/artifacts/*.md; do
        case "$(basename "$f")" in question.md|synthesized.md|verdict.md) continue ;; esac
        slug=$(basename "$f" .md)
        n=$(awk '/^## FINDINGS/{flag=1; next} /^## /{flag=0} flag && /^[-*]/' "$f" | wc -l | tr -d ' ')
        if [ "$n" -eq 0 ]; then
            quiet="$quiet${quiet:+, }$slug"
            continue
        fi
        total=$((total + n))
        # Prefix each line with a sort key: 1 HIGH, 2 MED, 3 LOW, 4 unrated. Unrated
        # sinks rather than floats — a panelist who skipped the rating did not thereby
        # make their finding urgent.
        awk -v who="$slug" '/^## FINDINGS/{flag=1; next} /^## /{flag=0}
             flag && /^[-*]/ {
                 sub(/^[-*][ \t]*/, "")
                 key = 4
                 if ($0 ~ /HIGH/)     key = 1
                 else if ($0 ~ /MED/) key = 2
                 else if ($0 ~ /LOW/) key = 3
                 printf "%d\t%s\t%s\n", key, who, $0
             }' "$f" >> "$raw"
    done

    {
        printf '## Findings\n\n'
        # -s keeps each panelist's own order inside one severity: they ranked their
        # own list, and re-sorting within a band would throw that away.
        sort -s -k1,1n "$raw" | awk -F'\t' '{ printf "F%d. %s — %s\n", NR, $2, $3 }'
        [ -n "$quiet" ] && printf '\nNo findings reported by: %s\n' "$quiet"
        printf '\n'
    } >> "$body"
    rm -f "$raw"

    # The head line last, because it counts what the body just found. SPAR_DELIVERED
    # and SPAR_EXPECTED come from the workflow, which is the only place that knows how
    # many panelists were ASKED — a panelist that died leaves no file, so counting
    # files cannot tell a silent failure from a smaller panel.
    {
        printf '# Panel verdict — GO %s · NO-GO %s · INSUFFICIENT %s' "$go" "$nogo" "$insuf"
        [ "$nover" -gt 0 ] && printf ' · no verdict %s' "$nover"
        printf '\n\n%s findings from %s panelist(s).\n\n' "$total" "$((go + nogo + insuf + nover))"
        # Written for the agent that opens this file without having read the skill.
        # Every rule here is one this panel has already been burned by.
        printf '%s\n' \
          'HOW TO READ THIS (rules, not suggestions):' \
          '' \
          '- Carry EVERY finding forward by its id. A finding one panelist made is not' \
          '  weak — it is the one nobody else saw. Merge two only if they are literally' \
          '  the same claim, never because they feel similar.' \
          '- Do NOT re-summarise this file. It is already the compression; the prose it' \
          '  came from is in the per-panelist files beside it.' \
          '- (R) means the panelist verified it — opened the file, ran the command.' \
          '  (G) is a guess. Check a (G) before acting on it; do not discard it.' \
          '- FIX: is the panelist'"'"'s proposed remedy, not an instruction. Judge it.' \
          '- Verdicts are independent opinions, not votes. One NO-GO with a reason' \
          '  outweighs four GO without one. INSUFFICIENT means that panelist could not' \
          '  see enough — read what it says it was missing.' \
          ''

        if [ -n "${SPAR_EXPECTED:-}" ] && [ "${SPAR_DELIVERED:-0}" -lt "${SPAR_EXPECTED}" ]; then
            printf '\n> **WARNING:** only %s of %s panelists delivered. This is a partial\n' \
                   "$SPAR_DELIVERED" "$SPAR_EXPECTED"
            printf '> panel — do not report it as a full one.\n'
        fi
        printf '\n'
        cat "$body"
    } > "$out"
    rm -f "$body"

    echo "$out"
    echo "spar-run: $total finding(s) collected verbatim — read the file, do not re-summarise it" >&2
    [ "$nogo" -gt 0 ] && echo "spar-run: $nogo panelist(s) said NO-GO" >&2
    return 0
}

case "${1:-}" in
    start)    shift; cmd_start    "$@" ;;
    wait)     shift; cmd_wait     "$@" ;;
    findings) shift; cmd_findings "$@" ;;
    *) die "usage: spar-run.sh start <question-file> [--mount ../repo]... | wait <run-dir> | findings <run-dir>" ;;
esac
