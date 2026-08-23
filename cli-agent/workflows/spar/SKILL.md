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
requires: medulla + docker (the panel runs in a container; `spar-run.sh` checks both
  before it starts and says which one is missing)
---

You're consulting an independent panel — the "братва" (the crew) — for
outside perspective on a non-trivial call you're working through. When
someone says "запусти братву", "собери братву" or "спроси у братвы",
they mean run this panel. Each member ("панелист") is one model in the
братва. The panel does not see this conversation, your codebase, or your
prior reasoning. Everything they need to give a useful answer has to be
in the prompt you build.

**Provoke them, do not align them.** These are not in tension, though they look it:
name your leaning as a TARGET ("we are leaning toward X — take it apart"), never as a
conclusion the panel is asked to bless ("we should do X, please critique"). The first
hands them something to attack; the second buys agreement. Same fact, opposite round.

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
- **Do not lead, and do not fence.** Two halves of one rule, and the second is the
  one people miss. Leading is telling them the answer: "we should do X, please
  critique" buys agreement, not scrutiny. Fencing is telling them where to look —
  listing the three things you want checked frames a round just as surely, because
  whatever is off the list gets looked at last or not at all, and the defect nobody
  thought to list is exactly the one worth a panel. So: state the raw facts, the
  conflicting constraints and the options as they stand; name the SURFACE (which
  change, which files, which callers) and ask what is wrong with it; put your own
  suspicions at the END, marked as yours. If a panelist returns something you never
  mentioned, the brief did its job.
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
- **Pick a shape, not a script.** These are starting points, not a menu:
   - Sparring with verdicts (COMMIT / DRAW / INSUFFICIENT) for
     binary or near-binary calls with stakes.
   - Multiple-lens roleplay (assign each panelist a distinct role —
     skeptic, devil's advocate, operator, contrarian) for relational,
     organizational, or judgment calls.
   - Red-team for plans about to execute.
   - Whatever else suits the situation.
- **Demands — only the ones your question needs.** The standing rules already reach
  every panelist through the workflow's own prompt: investigate before answering,
  no sycophancy, `(R)` for what you verified and `(G)` for what you guessed, report
  EVERY defect rather than the worst one, close with `## FINDINGS`. Do not restate
  them; you are spending the panel's attention twice. Add only what is specific to
  this question — the lens you want (skeptic, operator, red-team), the verdict shape
  you need, the blind spot you suspect is yours.

# Run the panel

    spar-run.sh start question.md                 # prints the run dir
    spar-run.sh start question.md --mount ../repo # repeatable, read-only
    spar-run.sh wait  <run-dir>                   # blocks until it finishes

The script lives beside the workflow — `.medulla/workflows/spar/scripts/spar-run.sh`
locally, `~/.medulla/workflows/spar/scripts/spar-run.sh` machine-wide. Write your
prompt to a file and hand it that file; nothing about the question passes through a
shell argument, so quoting, `$`, backticks and length stop mattering.

It is a script and not a command for you to reproduce because this repo's own
AGENTS.md says LLMs cannot be trusted to run exact commands — and the contract it
replaced had a heredoc, a background job, a poll loop and a `$PID` in it. The script
checks medulla, docker and the workflow before starting, keeps the run's history out
of the tree under review, adds the box to `.gitignore` (saying so), and refuses to
report a run directory it cannot vouch for.

`start` returns in about a second and the panel keeps working for 10-20 minutes. Do
not sit on it: go do other work, then `wait` — it blocks until `outcome.json` exists,
lists what was delivered, and exits non-zero if the run failed or never finished (45
minutes by default, `--timeout` to change). A hang and a verdict need different
reactions, so it distinguishes them.

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
