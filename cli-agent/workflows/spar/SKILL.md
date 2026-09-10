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

    q=$(mktemp -t spar-q).md                              # the brief lives in /tmp
    medulla launch spar start "$q"                        # prints the run dir
    medulla launch spar start "$q" --mount ../repo        # repeatable, read-only
    medulla launch spar wait  <run-dir>                   # blocks until it finishes

Call it through `medulla launch`, never by path. The script is a FILE, so a relative
path to it exists only where the workflow is installed: in a git worktree, in a sibling
repo, in any tree without its own copy, `.medulla/workflows/spar/scripts/spar-run.sh`
is "no such file or directory". `medulla launch spar` finds it by NAME through the same
cascade `-w` uses, from any directory. Write your prompt to a file and hand it that
file; nothing about the question passes through a shell argument, so quoting, `$`,
backticks and length stop mattering.

**A file beside the repository is INVISIBLE to the panel.** Only the working
directory becomes `/workspace`; a sibling path is not mounted and never was. Measured
live: a handout written to `../spar-handout/diff.txt` returned ENOENT inside the
container while `/workspace/.git` resolved fine, so the panel reviewed the tree with
no idea the handout existed. Put material the panel must read INSIDE the reviewed
tree, or pass it explicitly with `--mount` — which lands at `/workspace/<basename>`,
not at the path you typed.

**Write that file under `$TMPDIR` — never in the repo, never in `$HOME`.** `start`
copies the question into the run's own box before the panel ever reads it, so the file
you wrote is dead weight one second later. Left in the tree it is litter that outlives
the round by months: one workspace here accumulated 71 stray `panel-question-*.md`,
`panel-handout-*.diff` and `BRIEF.md` files, 292 MB of them, in its root. `start`
REFUSES a question file outside `$TMPDIR` for exactly this reason.

**But the question is the only file `start` copies.** Anything the panel must READ for
itself — a diff, a handout, an extracted scope, a `--mount`ed tree — has to sit on a
path the container runtime actually shares. On this machine Colima shares
`/Users/skopanev` and `/Volumes/hdd`, and NEITHER `/tmp` nor `$TMPDIR`
(`/var/folders/...`) is among them: Docker binds a missing source as an EMPTY directory
and says nothing, so the panel reads zero bytes and answers about nothing. A live round
lost its supplemental materials exactly this way. Put readable material under a shared
path — a scratch directory inside the repo's parent, or on the HDD — and keep only the
question itself in `$TMPDIR`.

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
failed or never finished. The default wait is the workflow's own deadline plus room to
conclude — not an independent number, so raising one moves the other; `--timeout`
overrides it. A hang and a verdict need different reactions, so it distinguishes them.

**A wait that returns is not proof the round ended.** Any bounded wait against a longer
job hands you a SNAPSHOT: the timeout can expire while the run is still legitimately
inside its budget. That was live — a wait gave up at 45 minutes on a container that
had every right to 60, and a lane recorded "timed out, no verdict" for a round that
had already met quorum. The constants are aligned now, but the shape of the risk is
permanent, so when a wait reports a timeout, read `outcome.json` before believing it.

List the artifacts with your Glob tool, then read them **one file at a time**:

    <Glob> "$run/artifacts/*.md"

Do not `cat` them all into the terminal: four panelists × ~500 words is a wall
of text that buries exactly the lone finding you are here to preserve.

Each panelist closes with two sections. `## FINDINGS` — one line per finding: `(R)`
confirmed or `(G)` guessed, `HIGH`/`MED`/`LOW` as the FINDER rates it, a `file:line`
or a concrete scenario, and a `FIX:` naming the function, the change and why. Then
`## VERDICT` — one line, `GO` / `NO-GO` / `INSUFFICIENT` and a reason. Both are the
machine-readable part: carry every line forward, attributed. `NONE` in FINDINGS is a
real answer, and so is `INSUFFICIENT` — a panelist who could not see enough says so
instead of dressing a guess as a decision.

**A verdict carries TWO clocks, and they disagree more often than you would think.**
`engine` dates the ROUND — it is written when the run starts. `collector` dates the
program that WROTE the verdict, which may be newer: a round begun before an upgrade
and synthesised after it carries an old engine with new fields. Measured across one
morning's runs, verdicts stamped with the same engine appeared on both sides of a
field's introduction — monotonic by write time, not by engine. So date a FIELD by
`collector` and the round by `engine`; telling a reader "this field exists from
version X" using the wrong clock makes them ignore it exactly where it is present.

**`attempts > 1` does not mean the attempt was vetoed.** A retry has more than one
cause, and only some are form refusals. Seen live: `attempts: 2` alongside `ok: true`,
`rc: 0`, `reason: "ok"` and an EMPTY message. Every actual veto carried `reason: post`
AND a `message` naming the rule, so read those two rather than the count.

**When a seat is missing from the round, the manifest says WHY in words.** One read
gives everything, and `input.slug` is the artifact's filename, so it leads straight to
the file to open by hand:

    jq -c '{ok, reason, message, attempts, slug: .input.slug}' steps/<NNN>-panel/manifest.jsonl

`reason` is `post` for every form veto and cannot tell them apart; `message` names the
rule that fired. Two seen live, and they are different problems with the same symptom:
`no FINDINGS section` is a heading the parser does not match (a panelist wrote
`## Находки`), while `VERDICT gives no reason` is a bare `GO` with nothing after it —
the panelist ignored the format, twice, and the retry produced the same bare word.
Note `body rc=0` in both: the panelist SUCCEEDED and the veto is about form only.

Do not read the row position as identity — it is not stable. One panelist sat at index
1 in two rounds and index 3 in the next.

**Panelists read the WORKING TREE, not a snapshot.** `/workspace` IS the directory
they open, live, for the whole round. So never run a PRODUCER — install, build,
codegen, formatter — in parallel with a panel: it writes into the bytes under review
while they are being read, and no sha records that. Measured: a build's `dist` was
written at 03:51 and the round launched at 03:51:52, and the tree the panel saw is one
nobody can rebuild, including the lane that made it. A dockerised test RUN is
survivable — its volumes are outside the tree and its artefacts are gitignored — but a
build is not. `git status` before and after both showed nothing, and both were
irrelevant: before and after are not during.

**Auditing a `clean` claim without re-running anything.** For a clean tree every term
after the head line is empty, so `reviewed_digest` collapses to a value anyone can
recompute offline:

    printf 'head:<reviewed_head>\n' | shasum -a 256

Equal means the round really did see a pristine checkout; different means the tree
carried something, whatever `reviewed_state` says. Useful for old runs, since it needs
no container and no repository.

**It answers two questions and not a third.** "Was it really clean" — recompute as
above. "Is this the same tree as that other round" — compare two RECORDED digests,
no reconstruction involved. But it does NOT tell you WHAT the dirt was, and trying to
find out is a trap: rebuilding a dirty digest means guessing the exact byte layout —
separators, whether a newline follows each filename, the order — and a mismatch cannot
distinguish "I guessed the layout wrong" from "the tree held more than I knew". A lane
tried it on dirt it could reproduce exactly (one untracked file, deterministic bytes),
missed with five plausible layouts, and correctly reported the miss as UNINFORMATIVE.
Read a failed reconstruction as nothing at all; a lane that reads it as evidence of a
stray writer has manufactured a false alarm.

If you do need to rebuild a dirty digest, here is the exact layout, so a mismatch means
what you want it to mean. Guessing it is what makes reconstruction useless:

    { printf 'head:%s\n' "$HEAD"
      git status --porcelain --untracked-files=all
      git diff HEAD
      git ls-files --others --exclude-standard -z \
        | xargs -0 -I{} sh -c 'printf "untracked:%s\n" "{}"; cat "{}"'
    ; } | shasum -a 256

Deterministic across runs on one tree, verified. Treat it as a contract: changing the
layout invalidates every recorded digest, so it moves only with a version bump.

**For a HISTORICAL run, read the recipe out of `<run-dir>/workflow.yaml` instead.**
Every run freezes its own definition there, so that file is the code that computed
THAT digest on THAT engine — a contract published later, against a different version,
is an assumption about an old artifact rather than a fact about it. The published
layout above is for what runs next.

Two details cost a lane five failed attempts, both worth knowing before you try:
the `untracked:<path>` prefix precedes each file's CONTENT, and `git ls-files
--others` prints paths RELATIVE to the repository — `spar-candidate.diff`, never
`./spar-candidate.diff`. Adding the `./` yields a different hash, so a correct
reconstruction looks like a failed one.

Done right, this answers the third question after all: WHAT the dirt was. A lane
rebuilt its dirty digest byte for byte and thereby proved its round read exactly one
commit plus one untracked file, with nothing writing during the read — any other file,
or a partial write, changes the hash.

**`<!-- spar-delivery-complete -->` is a PUBLIC contract, not an internal detail.**
Every panelist must close its artifact with that exact line, and the delivery hook
rejects an artifact without it. So a complete panel is assemblable WITHOUT the
aggregator: an artifact carrying the marker plus its own `## VERDICT` line is a
finished answer, whatever happened to `verdict.md`. A lane used exactly that to
recover a complete round after the collector never ran — better than the rule it
replaced ("no verdict.md means no result"), which would have reported a partial
forever on a panel that was whole. Lean on the marker; it is not going to move.

Rejected artifacts are kept, too. A retry writes to the SAME path, so the answer a
panelist gave first would otherwise vanish under its replacement — one reader saw two
different documents at one filename, with different finding ids and different
severities. The superseded copy lands in `artifacts/superseded/<slug>.<attempt>.md`,
outside the collector's glob.

`verdict.md` sorts the findings HIGH first and numbers them `F1`, `F2`… so you can
report back on each one by name.

**Then read every panelist's own file.** The artifacts directory holds one
`<slug>.md` per panelist — the argument behind their findings, which `verdict.md`
does not carry. Read them ALL. A finding exactly one panelist made is usually the
sharpest one, since only one of them saw it.

# Use the result

    <run-dir>/verdict.md     # written by the panel itself, as its last act
    <run-dir>/verdict.json   # the same facts for a gate: quorum, counts, blocking ids

**If something consumes this automatically, read `verdict.json`, not the Markdown.**
It carries the same pass: `state` (`CLEAR` or `REVIEW_REQUIRED` — branch on this),
quorum (expected, delivered, decided, met), the counts, blocking ids, verified HIGH
findings, each panelist's verdict with citations, every finding with its confidence and
severity, and `parser.malformed` naming any panelist whose file could not be read
properly — a delivered model is never silently omitted. It is written even when the round fails, because why it failed
is a fact too. Pass what you know about the subject and it comes back — `--var
TICKET=…`, `PURPOSE`, `BASE`, `HEAD`, `PATCH_DIGEST`; what you do not pass is simply
absent, never an empty string pretending to be an answer.

**`BLOCKING:` is the line you act on.** It lists the findings every NO-GO actually
rests on, by id. The verdict counts above it are NOT a vote: `GO 2 · NO-GO 2` is not a
tie to break by counting — it is two findings to judge against their cited code. A
landing went through on exactly that arithmetic, because nothing tied the two NO-GO
verdicts to anything checkable. A NO-GO that cites no finding is printed as an opinion
and blocks nothing; a cited one blocks until you write down what refutes it.

Read `verdict.md` FIRST — the panel writes it as its last act, so it is there before
you ask, and it is written by the workflow itself rather than by a tool that has to be
found (a tool that has to be found is a tool that can be missing: it was, once, and the
run reported success having written nothing). It opens with every panelist's verdict together — `GO 1 · NO-GO 1
· INSUFFICIENT 1` — because the SPLIT is the answer to "can we ship", and four files
each ending in one word are unreadable as four files and obvious as one block. A
panelist who skipped the section shows as `(no verdict section)` rather than being
counted as agreement. Asking a summariser not to summarise is asking water to be dry:
a model that reads every artifact and retells them will drop the lone finding — which
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
