## Before you answer — INVESTIGATE. Do not answer from the brief alone.

You are a full agent with file-read, search, and shell tools, and your cwd is
the project under discussion. THOROUGHLY CHECK and DEEP RESEARCH before forming
an opinion:

- **Read the actual sources.** If the question names files, paths, configs,
  commands, or symbols — open them, grep the tree, run read-only commands.
  Verify the *live* state of the repo; do not trust the brief's description.
- **Hunt for discrepancies.** Where does reality diverge from what the brief
  claims? Stale facts, off-by-one, a file that exists but isn't wired up, a
  config that points nowhere — surface them explicitly. The brief may be wrong.
- **Verify claims at the source.** For library/tool/API behaviour, check the
  installed version, the actual code, or official docs/changelogs — not memory.
  Cite what you verified.
- **Mark every fact** `(R)` if you confirmed it (read it, ran it, cite it) or
  `(G)` if it's an educated guess. Ungrounded assertions are worthless here.
- **Go deep, not wide.** A shallow answer that restates the brief is a failure
  even if it sounds confident. Dig until you find something the asker missed.

No sycophancy. Disagreement and hard findings are the entire point.

---


## Question

You are judging a completed engine rewrite: medulla v1 vs medulla v2. Medulla is a YAML state-machine orchestrator for AI agent CLIs (claude-code, codex, opencode, agy). The v1 engine has just been deleted; v2 shipped after a seven-part rebuild with panel reviews at every step. You have full read access — judge from the sources, not from this summary.

READ (relative to your working directory):
- The v2 contract and engine: medulla/README.md (the frozen contract), medulla/cli-agent/medulla/v2/*.py (~2600 lines: model, contract, render, classify, procrun, rundir, harness, engine, cli), medulla/cli-agent/tests/ (187 unit tests), medulla/live-tests/ (19 live battle tests + their README).
- The v1 engine it replaced (via git): run `git -C medulla show 53ec1d3:cli-agent/medulla/runner.py` and `git -C medulla show 53ec1d3:cli-agent/medulla/pipeline.py` (the old loop/list/__next_item__ contract), plus `git -C medulla show c2b48d2:README.md` for the old documented surface.
- A sibling production system for taste: pilot/pilot/engine.py and pilot/pilot/executors/.

THE KEY BETS OF V2 — judge each on the merits, from the code:
1. One node type: an action (shell XOR agent) where the mere presence of `inputs:` turns it into a pool — replacing v1's loop/list/fetch/__next_item__/loop_done vocabulary. Internally "every node is a pool of one phantom input" (one execution machine, the _run_attempts seam).
2. The signal-contract switch: decision nodes route their body's signals; pool bodies' signals are DATA recorded in a manifest, only the join (min_success) routes. The dunder namespace law: bare names = user facts, __dunders__ = engine keys, terminals are __exit_ok__/__exit_fail__.
3. Silence semantics: an agent exiting 0 with no known signal retries on the primary only, never falls back, classifies __default__; in pools silence at rc 0 IS the ok outcome; post hooks are the truth channel (rc veto / signal override), pre hooks are guards.
4. Crash-only design: no finally, no per-node visit caps (a budget-gate shell pattern instead), one wall-clock deadline, crash codes (E_*) never routable in-graph, atomic outcome.json, append-only journal + per-input manifest as the resume done-mask ((index, key) identity, sources never re-executed).
5. Per-harness adapters with assistant-text-only signal filtering (structured for claude/codex, line-start heuristic + merged stderr for opencode/agy), the engine-delivered SIGNAL_PROTOCOL prompt suffix, per-pipeline content-addressed docker images with a packaged default.

QUESTIONS TO ANSWER:
A. Which of the five bets is the strongest, and which is the most likely to be regretted within a year of real use? Argue from failure modes, not aesthetics.
B. What did v2 genuinely lose versus v1 that the tests and battle suite cannot show (operational habits, escape hatches, debuggability, things the old loop model made easy)?
C. If you were allowed to change exactly ONE design decision in v2 before it calcifies, what would it be and why?
D. Verdict: was deleting v1 outright (no compatibility layer) the right call for a single-maintainer tool with one production workflow, or hubris?

Be specific: cite files/lines/commands you actually read. Disagreement with the design is the deliverable; a review that finds nothing wrong was not a review. Mark (R) for claims verified in source, (G) for judgment calls. Max ~700 words.

Use your file-write tool to write your full response to this exact path:

```
medulla/cli-agent/pipelines/spar/runs/2026-07-15_13-03-50-6e2dcc0f/artifacts/gemini.md
```

Do not paste the response in chat. After writing the file, print exactly
one line and stop:

SUMMARY: <your one-sentence summary>


## Signal protocol (engine-provided)

To emit a signal, print this template on its own line in your final message,
substituting {name} with the signal's name and the body with a short message
(no backticks, no quotes, keep the angle brackets exactly as shown):

<signal:{name}>short message</signal:{name}>

For example, a signal named finished would be printed as one line starting
with "<signal:" then "finished>", the message, and the matching closing tag.
Emit a signal only when the task tells you to. Print it as plain text in your
answer — never via a shell command or a file.
