---
name: spar
description: >-
  A panel of independent models attacks one decision in parallel — counsel without
  consensus. For calls that facts cannot settle: strategic, architectural,
  organizational, product. Wrong tool if a lookup or a code change would answer it.
  Called "братва"; "запусти братву" means run this.
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
- **Sibling repos — mount them, and name them the way the panel sees them.** For code
  outside this workspace: `medulla launch spar start q.md --mount ../other-repo` (repeatable,
  read-only). Inside the container it appears at **`/workspace/other-repo`** — the
  basename, under /workspace — NOT at `../other-repo`, which does not exist there. Point
  the panel at the container path, or they burn a turn on "No such file or directory".
  The launcher prints the mapping for every mount when it starts.

- **Demands — only the ones your question needs.** The standing rules already reach
  every panelist through the workflow's own prompt: investigate before answering,
  no sycophancy, `(R)` for what you verified and `(G)` for what you guessed, report
  EVERY defect rather than the worst one, close with `## FINDINGS`. Do not restate
  them; you are spending the panel's attention twice. Add only what is specific to
  this question — the lens you want (skeptic, operator, red-team), the verdict shape
  you need, the blind spot you suspect is yours.

# Run the panel

    medulla launch spar start question.md                 # prints the run dir
    medulla launch spar start question.md --mount ../repo # repeatable, read-only
    medulla launch spar wait  <run-dir>                   # blocks until it finishes

Call it through `medulla launch`, never by path. The script is a FILE, so a relative
path to it exists only where the workflow is installed: in a git worktree, in a sibling
repo, in any tree without its own copy, `.medulla/workflows/spar/scripts/spar-run.sh`
is "no such file or directory". `medulla launch spar` finds it by NAME through the same
cascade `-w` uses, from any directory. Write your prompt to a file and hand it that
file; nothing about the question passes through a shell argument, so quoting, `$`,
backticks and length stop mattering.

It is a script and not a command for you to reproduce because this repo's own
AGENTS.md says LLMs cannot be trusted to run exact commands — and the contract it
replaced had a heredoc, a background job, a poll loop and a `$PID` in it. The script
checks medulla, docker and the workflow before starting, keeps the run's history out
of the tree under review entirely — it lands in `~/.medulla/panel-runs/<repo>-<hash>/`,
so the repository you are reviewing gains no directory and no `.gitignore` line — and
refuses to report a run directory it cannot vouch for.

`start` returns in about a second and the panel keeps working for 10-20 minutes. Do
not sit on it: go do other work, then `medulla launch spar wait <run-dir>`. It blocks
until `outcome.json` exists, lists what was delivered, and exits non-zero if the run
failed or never finished (45 minutes by default, `--timeout` to change). A hang and a
verdict need different reactions, so it distinguishes them.

List the artifacts with your Glob tool, then read them **one file at a time**:

    <Glob> "$run/artifacts/*.md"

Do not `cat` them all into the terminal: five panelists × ~500 words is a wall
of text that buries exactly the lone finding you are here to preserve.

Each panelist closes with two sections. `## FINDINGS` — one line per finding: `(R)`
confirmed or `(G)` guessed, `HIGH`/`MED`/`LOW` as the FINDER rates it, a `file:line`
or a concrete scenario, and a `FIX:` naming the function, the change and why. Then
`## VERDICT` — one line, `GO` / `NO-GO` / `INSUFFICIENT` and a reason. Both are the
machine-readable part: carry every line forward, attributed. `NONE` in FINDINGS is a
real answer, and so is `INSUFFICIENT` — a panelist who could not see enough says so
instead of dressing a guess as a decision.

`verdict.md` sorts the findings HIGH first and numbers them `F1`, `F2`… so you can
report back on each one by name.

**Then read every panelist's own file.** The artifacts directory holds one
`<slug>.md` per panelist — the argument behind their findings, which `verdict.md`
does not carry. Read them ALL. A finding exactly one panelist made is usually the
sharpest one, since only one of them saw it.

# Use the result

    <run-dir>/verdict.md     # written by the panel itself, as its last act

Read `verdict.md` FIRST — the panel writes it as its last act, so it is there before
you ask, and it is written by the workflow itself rather than by a tool that has to be
found (a tool that has to be found is a tool that can be missing: it was, once, and the
run reported success having written nothing). It opens with every panelist's verdict together — `GO 1 · NO-GO 1
· INSUFFICIENT 1` — because the SPLIT is the answer to "can we ship", and five files
each ending in one word are unreadable as five files and obvious as one block. A
panelist who skipped the section shows as `(no verdict section)` rather than being
counted as agreement. Asking a summariser not to summarise is asking water to be dry:
a model that reads five artifacts and retells them will drop the lone finding — which
is the one the panel was convened for. `awk` has no opinions, so the collection is
mechanical and the count is printed. Carry those lines forward as they are, attributed;
merge two only when they are literally the same claim, never when they "feel similar".

Then read the artifacts themselves, one file at a time — the argument above each list
carries the reasoning that `verdict.md` cannot. What to look for:

1. Where they converge — rare, and worth flagging when it happens.
2. Where they diverge, and which divergence actually decides your question.
3. What all of them missed, because none of them saw your conversation or your
   constraints. You are the only one who can notice that.
4. **Use the `FIX:` clauses — they were written FOR you.** The panel provides
   mechanical fixes (functions, lines, commands) specifically to save your time. Pick
   the one that fits and apply it; re-deriving a remedy the panel already worked out
   turns a 20-minute run into a 40-minute one.

   **Using is not obeying:**
   - A `(G)` fix is a guess — verify the claim before applying it.
   - Incompatible fixes for one defect mean the *fix* is the arguable part, not the
     defect. Decide which one to use, and say why.
   - You alone see the full context and constraints. Apply what fits, adapt what
     nearly fits, and explicitly state which fixes you rejected and on what evidence.

A `WARNING: only N/M panelists delivered` line means partial delivery — somebody died
or a provider refused. Say so when you report; never present a partial panel as a full
one. Do not soften a verdict to fit what you already thought: the disagreement is the
signal, and it is the entire reason this costs twenty minutes.
