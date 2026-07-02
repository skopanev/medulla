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
- **Files in the repo (use them).** The panel runs as full agents —
  claude-code, codex, opencode — each with file-read tools and the
  project workspace as their cwd (the panel runs in Docker: paths
  outside the workspace are NOT visible to it). Files matter not
  just for the *answer* but for the *context the panel needs to
  answer well*: the code the question is about, strategy or
  architecture docs, prior decisions, design notes, README files.
  **Instruct the panel to read what's relevant** — be concrete about
  file paths, line ranges, grep patterns. Don't paste large snippets
  into the prompt when you can point at the source; pasting wastes
  tokens the panel could spend reasoning.
- **Framing.** Pick what fits:
   - Sparring with verdicts (COMMIT / DRAW / INSUFFICIENT) for
     binary or near-binary calls with stakes.
   - Multiple-lens roleplay (assign each panelist a distinct role —
     skeptic, devil's advocate, operator, contrarian) for relational,
     organizational, or judgment calls.
   - Red-team for plans about to execute.
   - Whatever else suits the situation. These are starting points,
     not a menu.
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
