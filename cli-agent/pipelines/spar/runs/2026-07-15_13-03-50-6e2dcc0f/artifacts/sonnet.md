# medulla v2 vs v1 — engine review

## A. Strongest bet / most likely regret

**Strongest: bet 2, the signal-contract switch.** Pool bodies write to a
manifest, only `min_success` at the join routes (`engine.py:368-379`,
`contract.py:212-216` rejects bare signals in pool `on_signal`). This closes
a real v1 failure mode: v1's `_run_loop_parallel` decided per-item success by
scanning for `next_item_signals` membership *or* `rc == 0`
(`/tmp/v1_runner.py:180-186`) — an agent that emitted a stray signal-shaped
string in tool output could silently flip an item to done. v2's law ("pool
signals are DATA, never route", enforced at load time, not by convention) is
a structural fix, not a lint rule. (R)

**Most likely to be regretted: bet 3, agent silence retries primary-only,
never falls back** (`classify.py:80-89`, README:117). The stated reason —
"another model drops the tag just as often; blind fallback duplicates side
effects" — is a real risk, but it's asymmetric: it protects against a
double-write on a *stateful* body while guaranteeing that a *primary model
having a bad day* (rate-limited, degraded, mis-following instructions) drains
its entire `max_attempts` budget on itself and then hard-fails via
`__default__`, even though the whole reason `fallback` exists is "primary is
unreliable, try someone else." A budget-gate retry-node can route around
`__failed__`, but `__default__` is a distinct terminal with its own edge
(README:340, `defaults.on_signal: {__failed__: notify, __default__: notify}`)
— so silence-driven exhaustion needs its *own* handling, which most authors
will forget to add, because `fallback:` visually reads as "the safety net for
this action" and it silently isn't one for the single most common agent flake
class (README:117 calls silence "the most common agent flake"). One year of
production use with prompts that drift or model updates that change tagging
habits will surface pipelines that fail hard on a fixable communication
glitch, precisely the class of failure `fallback` was built to catch. (R for
the code path; G for the year-out prediction)

## B. What v2 lost that tests can't show

1. **Live terminal UX during a run.** v1's `output.py` (imported by
`runner.py:9-13`) gave colored round banners, transition arrows, per-item
parallel status lines (`ansi(..., BOLD+GREEN)`, `round_banner`,
`round_stats`). v2's `log()` (`engine.py:56-57`) is one bare
`[medulla] step N | node -> signal -> target` line to stderr. Functionally
equivalent for the journal; materially worse for a human babysitting a long
run at a terminal — no coverage in the unit or live-test suite because none
of it asserts terminal *output*, only structured outcomes. (R)

2. **Cross-invocation persistent vars + real SIGTERM cleanup.** v1's
`vars_map` synced to `.medulla/vars.<task>.yaml` on disk independent of any
run directory (`load_vars`/`save_var`, `runner.py`), and registered
`atexit`/`SIGTERM` handlers (`_cleanup_bridge_bg`, `runner.py:200-270`) to
kill background dev servers, stop docker containers, and clean bridge
sockets on a *graceful* kill, not just SIGKILL. v2 explicitly deletes this
("no finally... `kill -9` makes exit hooks an illusion", README:306) and
only catches `KeyboardInterrupt` (`engine.py:941-944`) — SIGTERM is
unhandled, so a pipeline that legitimately backgrounds a process (dev
server, emulator) and gets `docker stop`/systemd-SIGTERM'd leaks it. The
crash-only philosophy is defensible against `kill -9`, but SIGTERM was
graceful in v1 and is now treated identically to SIGKILL. The live-test
suite only checks SIGINT (`t14-interrupt`), not SIGTERM, so this gap is
invisible to it. (R)

3. **Inline per-signal steps.** v1 allowed a transition to carry inline
`runner:` steps or `reset_iterations` alongside the stage jump
(`pipeline.py:70-79`, `_resolve_signal_target`). v2 forces every such action
into its own node+edge (README's "handlers are ordinary nodes"). Cleaner
contract, but it inflates small pipelines' node count for what used to be a
one-line hook — not tested either way since it's a authoring-ergonomics
delta, not a behavior delta.

## C. One change before it calcifies

Split `__default__`'s routing from `__failed__`'s: **let silence use the
fallback runner, gated behind an explicit opt-out**, rather than hard-wiring
"primary only, never fallback" as the only mode. The manifest/journal already
record `reason` per failure class (`recorded_signal`, `failure_class` in
`AttemptsOutcome`), so an author who genuinely needs the current
duplicate-avoidance guarantee (mutating body, side-effect-heavy) could opt in
to "no fallback on silence," while the default serves the common case
(idempotent/read-mostly agent bodies) where blind fallback on silence is
obviously better than a hard stop. Right now the contract picked the
conservative branch for everyone and calls it done in README's frozen
contract — that's the one clause I'd unfreeze.

## D. Verdict: deleting v1 outright

**Right call, not hubris — for the actual conditions stated.** Single
maintainer, one production workflow, and a *documented* migration table
(README:317-337) mapping every v1 primitive to its v2 equivalent is the
opposite of hubris; it's the correct cost/benefit call when running a
compatibility shim (dual engines, dual test suites, dual mental models)
costs more than porting the one live pipeline by hand once. The panel-review
process (audit references littered through the code — "audit R1", "audit
R3", "audit G7", "audit G8", "audit G9" in `harness.py`, `procrun.py`,
`engine.py`) shows the deletion was preceded by actually re-deriving v1's
scars (agy `--print` token-eating, opencode's dead `--format json`, the
`kill -9` orphan bug from `t14`) rather than dropping them. The one place
this reads as overconfidence rather than diligence is **bet 3** above: the
contract document calls itself "frozen" while shipping a debatable one-way
door on silence-handling that the authors themselves flag as "the most
common agent flake" — freezing a contract *before* a full production season
under real model-drift conditions is the part that could bite. (G)
