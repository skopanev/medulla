---
name: spar
description: |
  A panel of independent models attacks the same decision in parallel —
  counsel without consensus, scrutiny without sycophancy. Each returns
  reasoning, verdict, and the strongest counter to whatever's being
  leaned toward. For decisions that can't be confirmed by facts:
  strategic, architectural, organizational, career, product. The panel
  exists to disagree. If a fact lookup or a code change would resolve
  the question, this is the wrong tool. Colloquially this panel is the
  "братва" (the crew) — "запусти братву" / "спроси братву" means run
  this skill; a "панелист" is one member of the братва.
effort: high
---

You're consulting an independent panel — the "братва" (the crew) — for
outside perspective on a non-trivial call you're working through. When
someone says "запусти братву", "собери братву" or "спроси у братвы",
they mean run this panel. Each member ("панелист") is one model in the
братва. The panel does not see this conversation, your codebase, or your
prior reasoning. Everything they need to give a useful answer has to be
in the prompt you build. **DO NOT TRY TO CONVINCE THEM OF ANYTHING. YOUR
JOB IS TO PROVOKE THEM, NOT TO ALIGN THEM.**

# Build the prompt

The prompt is the artifact. Treat it as the brief you'd give a
senior outsider you trusted: complete enough to answer without
follow-up, sharp enough to provoke real thinking.

Cover:

- **The problem.** What's actually being decided. State it precisely.
- **Progress so far.** What you've tried, what you've ruled out,
  what you currently think and why. Make your leaning explicit so
  the panel can attack it.
- **The stuck point.** Where your reasoning runs out, what data
  you don't have, what you genuinely cannot decide alone.
- **Zero agent bias (CRITICAL).** You, the agent writing this prompt, MUST NOT
  solve the problem in advance and MUST NOT lead the witness. Do not pre-digest
  the situation so your preferred answer looks like the only logical one. State
  the raw facts, the conflicting constraints, and the options as they actually
  stand. "I think we should do X, please critique" is a failure: it buys
  agreement, not scrutiny. Let the panel do the thinking.
- **Files in the repo (point, don't paste).** The panel runs as full
  agents with file-read and search tools. Point them at files and
  directories — "look at `src/payments/`, the deposit handler, the
  infra config for staging" — and let them dig. Don't paste specific
  lines or snippets; that wastes tokens and anchors them on what you
  think matters. Give direction, not extracts.
- **Sibling repos (mount them read-only).** If the question involves
  code outside the current workspace: `--mount ../folder1 [--mount ../folder2] ...`
  Point the panel at them in the prompt — they can't search what
  isn't mounted.
- **Framing.** Give the raw situation, not your solution — build an arena for
  them to fight in, do not hand them a script. Your ideas
  belong in the brief — but as options to consider, not as the only
  path. Leave room for the panel to say "you're solving the wrong
  problem" or "here's an option you didn't consider." If you hand
  them a fully-formed plan, they'll critique the plan instead of
  questioning whether it's the right plan. If you do pick a format,
  these are starting points, not a menu:
   - Sparring with verdicts (COMMIT / DRAW / INSUFFICIENT) for
     binary or near-binary calls with stakes.
   - Multiple-lens roleplay (assign each panelist a distinct role —
     skeptic, devil's advocate, operator, contrarian) for relational,
     organizational, or judgment calls.
   - Red-team for plans about to execute.
   - Whatever else suits the situation.
- **Demands.** These are words you WRITE INTO THE PROMPT — not medulla settings,
  not workflow config. The panelists only obey what your prompt says, so spell
  out: depth, dissent, the strongest counter, the blind spot you're least likely
  to see, no sycophancy, mark each cited fact `(R)` for confirmed or `(G)` for
  guess. Aim ~700 words — but a REPRODUCTION BEATS BREVITY: never truncate the
  steps that show HOW a defect happens. The `## FINDINGS` list is the compression;
  the argument above it may run long when it is carrying evidence.

# Run the panel

No preflight needed: a repo without its own copy falls back to the machine-wide
definition in `~/.medulla/workflows/spar/`. A local `workflow.yaml` wins if present.

Mechanical contract — invoke verbatim, substituting your built prompt
for the heredoc body (quoted 'EOF' keeps `$(...)`, backticks and
quotes in the prompt inert). Add `--mount ../repo` (repeatable, read-only) for
every sibling repo the panel must be able to read:

    BOX="$PWD/.medulla/panel-runs"        # in the project, beside it, never in /tmp
    mkdir -p "$BOX"
    cat > "$BOX/question.md" <<'EOF'
    <your prompt>
    EOF
    medulla --print-run-dir --docker --cwd-ro --runs-folder "$BOX" \
      -w .medulla/workflows/spar [--mount ../repo] \
      --var-file "QUESTION=$BOX/question.md" >"$BOX/run.log" 2>"$BOX/err.log" &
    PID=$!
    # The run dir is printed at startup, but under --docker that is ~20s away (the
    # container upgrades medulla first). Watch the PROCESS too: if medulla dies before
    # printing — bad yaml, Docker not running — waiting on the file alone hangs forever.
    while [ ! -s run.log ] && kill -0 $PID 2>/dev/null; do sleep 1; done
    if [ ! -s run.log ]; then
        echo "ERROR: medulla failed to start:"; cat err.log; exit 1
    fi
    run=$(head -1 run.log)
    echo "panel running in background (pid $PID), run dir: $run"

**Run it from the project ROOT.** That directory is what gets mounted as
`/workspace`, what `-w .medulla/workflows/spar` is resolved against, and what `$BOX`
is named after — start from a subdirectory and the panel reviews a slice of the repo
under a box named after that slice. The root is the launch point even when the
question is about one file deep inside it.

**Why the flags, and why $BOX.** A panel reads the tree and must not write it —
three rounds left something behind (a dangling symlink, a file staged in the agent's
index, a stray `test.sh`), and at review time none of it is distinguishable from real
work. `--cwd-ro` mounts the workspace read-only; `--runs-folder` gives the run
somewhere else to write, and it is required — read-only alone would leave the run
nowhere to go. The shell redirects go to `$BOX` for the same reason: they are written
by the HOST shell, so `--cwd-ro` cannot stop them landing in the repo.

**The question travels as a FILE.** `--var-file QUESTION=…` reads it straight off
disk, so nothing about it passes through a shell argument: no quoting to get wrong, no
length limit to hit, and every newline arrives as written. A shell variable that lost
its content sets an EMPTY question and the panel spends ten minutes answering nothing —
an empty file is refused outright instead.

`$BOX` lives **in the project**, at `.medulla/panel-runs/`. It is mounted writable at
its own path, so it stays writable while everything around it is read-only, and the
run directory printed by `--print-run-dir` opens on the host unchanged. Do not put it
in `/tmp`: the VM shares only part of the filesystem, and a path it cannot see is
mounted as an empty root-owned directory instead — medulla checks for this and refuses
to start, but the run you wanted is still not running. Add `.medulla/panel-runs/` to
`.gitignore` once per repo.

This script returns in ~20s with the run dir; the panel keeps working in the
background for its 10-20 minutes. **Do not sit and wait on it** — go do other
work and come back for the artifacts.

**Do not paste `--mount` away.** If the question touches code outside this
workspace, the mounts belong in the command above — a panel that cannot read a
repo will confidently reason about it from the brief alone.

When the run finishes, LIST the artifacts and then read them **one file at a
time with your own file-reading tool**:

    ls "$run/artifacts"/*.md

Do not `cat` them all into the terminal: five panelists × ~500 words is a wall
of text that buries exactly the lone finding you are here to preserve.

Each panelist closes with a `## FINDINGS` list — one line per finding, `(R)`
confirmed or `(G)` guessed, with a `file:line` or a concrete scenario. That list
is the machine-readable part: carry every line forward, attributed. If a
panelist's FINDINGS says `NONE`, that is a real answer, not a failure.

**Read every panelist's own file, not only `synthesized.md`.** The artifacts
directory holds one `<slug>.md` per panelist plus the combined
`synthesized.md`. Read them ALL. Skimming the synthesis is how a finding that
exactly one panelist made — usually the sharpest one, since only one of them
saw it — gets dropped.

A `WARNING: only N/M panelists delivered` line means partial delivery: someone
died or their provider refused. Say so when you report; do not present a
partial panel as a full one.

# Use the result

You called the panel because you needed perspective. Now you have
takes from outside your context. Read them, then:

1. Notice where the panel converges — rare; worth flagging.
2. Notice where they diverge, and identify which divergence most
   matters for the specific decision you're working through.
3. Notice what the panel collectively missed because they didn't
   see your conversation, your codebase, or your actual constraints.

**Carry EVERY concrete finding forward — never average the panel.** The value
is in the union of what they found, not the consensus. A point made by one
panelist out of five is not a weak point; it is the one nobody else saw. When
you report back, keep each distinct finding as its own item, attributed to who
raised it, with its file:line or concrete scenario intact. Merge two items only
when they are literally the same claim — not when they merely "feel similar".
Dropping a lone finding because the others did not repeat it is the single
failure mode of this whole tool.

Do not soften their verdicts when integrating into your reasoning.
Do not flatten dissent into consensus through restatement. The
disagreement is the signal — the entire point of running this.
