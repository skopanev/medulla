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
in the prompt you build.

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
- **Framing.** Give the situation, not just your solution. Your ideas
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
- **Demands.** Depth, dissent, the strongest counter, the blind
  spot you're least likely to see. Forbid sycophancy. Mark cited
  facts `(R)` for confident recall, `(G)` for guess. Cap each
  panelist at ~500 words.

# Run the panel

Mechanical contract — invoke verbatim, substituting your built prompt
for the heredoc body (quoted 'EOF' keeps `$(...)`, backticks and
quotes in the prompt inert; `--stage prepare` is mandatory — it
prevents a stale interrupted run from answering the previous question):

    QUESTION="$(cat <<'EOF'
    <your prompt>
    EOF
    )"
    medulla --docker -w .medulla/workflows/spar --stage prepare --var "QUESTION=$QUESTION" >&2
    cat "$(ls -td .medulla/workflows/spar/runs/* | head -1)/synthesized.md"

The cat'd output is the panel's combined takes, separated by headers.
A `WARNING: only N/4 panelists delivered` line means partial delivery —
weigh the takes accordingly.

# Use the result

You called the panel because you needed perspective. Now you have
takes from outside your context. Read them, then:

1. Notice where the panel converges — rare; worth flagging.
2. Notice where they diverge, and identify which divergence most
   matters for the specific decision you're working through.
3. Notice what the panel collectively missed because they didn't
   see your conversation, your codebase, or your actual constraints.

Do not soften their verdicts when integrating into your reasoning.
Do not flatten dissent into consensus through restatement. The
disagreement is the signal — the entire point of running this.
