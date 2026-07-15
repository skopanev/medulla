# glm5

# v2 engine review — judged from the sources

## A. Strongest bet, and the one most likely to be regretted

**Strongest: Bet 4 (crash-only + atomic outcome + append-only manifest).** Verified in `rundir.py:136-139` (tmp+replace outcome write), `rundir.py:123-127` (single-write manifest append → crash can only truncate the tail), and `engine.py:674-680` (resume done-mask keyed on `(index, key)` identity, with sources never re-executed — `engine.py:519-525`). This is the one bet whose failure mode (partial-state corruption after `kill -9`/OOM/E_DEADLINE mid-pool) is unrecoverable, and the design actually closes it: deadline-killed inputs leave no row so they re-run (`engine.py:694-699`), and concluded inputs keep their manifest rows even when a sibling worker crashes (`engine.py:727-730`). For an orchestrator whose reason-to-exist is "runs for hours and gets interrupted," durability IS the product. Everything else is recoverable.

**Most likely regretted: Bet 3 (silence semantics — agent silence retries primary only, never falls back).** The rule lives in `classify.py:87` (`if kind == "agent" and phase == "primary" and attempt < max_attempts: RETRY_SAME`) and the justification is in README §117 ("another model drops the tag just as often; blind fallback duplicates side effects"). The failure mode it forces: when the primary is silently failing repeatedly (truncated context, broken tools state, a prompt the model has stopped honoring), `max_attempts` exhausts → `__default__` → `__exit_fail__` (`model.py:146-147`) — a hard stop with **no in-graph escape**. The endorsed workaround (`post:` shell veto, README §130-136) cannot help because `post` is shell-only and cannot rerun work on another model. So the most common agent flake — work happened, tag didn't — has exactly one recovery path: human edits the pipeline. Within a year of daily use this is the rule that will page you at 2 AM and you'll add a `fallback_on_silence:` opt-in under pressure. Compound risk: the same `Verdict.SILENT` value means opposite things — failure on decision nodes, success on pools (`classify.py:81-84`) — keyed only on a `pool_mode` boolean threaded through `_run_attempts`. One missed flag on a new code path = catastrophic misrouting. Latent bug magnet. (R)

## B. What v2 genuinely lost vs v1 (the tests can't show any of this)

1. **Real-time signal streaming.** v1's `signal_cb=on_realtime_signal` (`executor.py:39`, `runner.py:~412`) extracted signals *during* stdout streaming and could `killpg` on first hit. v2's `procrun.py:4-7` explicitly kills this ("no signal callback... full body output is captured"). For a 30-minute opus run you now wait for process exit to see ANY signal — a long blind window. Tests can't show it (fake harness, sub-second scripts). (R)
2. **`finally:` per stage.** v1 had `finally_runner` (`runner.py:~444, 695`); v2 removed it on the "kill -9 makes exit hooks an illusion" argument (README §306). Correct in principle, but v1's own `_cleanup_bridge_bg` (`runner.py:~150-220`: docker stop, kill_pgid, kill_pidfile) is the real-world need — non-idempotent teardown of long-lived side processes started in node A and needed dead before node B. v2's "clean idempotently at entry" assumes the cleanup *can* be made idempotent; reusing a PID file is genuinely dangerous. No live test starts a side process in one node and asserts it's dead before the next.
3. **First-class `max_iterations` / `on_max`.** v1 had a per-stage cap (`runner.py:~415`); v2 replaced it with the author-authored budget-gate shell pattern (README §296-303) and removed the global `max_rounds=500` safety net v1 had (`runner.py:~357`). Author discipline under load is not a control. (R)
4. **Per-item `fetch:` enrichment.** v1's loop ran `fetch:` per item to lazily load heavy data (`runner.py:~617`). v2 forces the producer to emit rich objects eagerly. A producer listing filenames + per-item DB lookup now must do all lookups upfront or spawn an extra pool.
5. **Signal-body hook steps.** v1's `on_signal:` could be a list of `{runner: ...}` steps run before transition (`runner.py:~58-73`). v2 forces every side effect to be a full node with its own timeout/attempts/journal row (README §230). For "one curl on the way out" that's noticeably heavier.
6. **`default:` as a user route.** v1 let silence route to ok (`default: __exit__`). v2 hard-wires `__default__ → __exit_fail__` (`model.py:146-147`) — every shell decision node MUST echo a signal to succeed. A sharp edge dressed up as hygiene. (R)

## C. The one decision I'd change before it calcifies

**Re-introduce a per-node visit cap, overridable to unlimited.** The README is explicit and proud (§295): "the engine has no per-node visit caps; cycle semantics belong to the workflow." Failure mode: a routing typo (A→B, B→A) is completely undetected — no warning, just a run that burns API credits until the wall-clock deadline (default 86400s = thousands of agent calls). Detection is trivial: `dict[node, int]` and one check at the top of `run()`'s loop (`engine.py:795`). The budget-gate pattern puts the entire burden on author discipline — and a single maintainer writing pipelines under deadline pressure will, eventually, forget it exactly once. v1 had `max_rounds=500` as a net; v2 deleted it for purity. Of all five bets this is the one most likely to produce a "$400 overnight run" story, and the cheapest to prevent. Once pipelines are written assuming no cap, adding one is a breaking change — so this calcifies fast.

## D. Verdict on deleting v1 outright

**Right call, not hubris — given the actual constraints** (single maintainer, one production workflow, version-gated). Verified: the v2 validator rejects anything but `version: "2"` with a migration pointer (`contract.py:249-253`); the migration table (`README §321-337`) maps every v1 construct; old v1 run directories simply don't share layout with v2 (`rundir.py` journal/manifest/outcome), so there's no mixed-format bridge to maintain; the 20 live battle tests cover the surfaces that actually bite (adapters, fallback, resume mid-pool, deadline, SIGINT, concurrency, dirty data, empty source — `live-tests/README.md`). A compat layer would have meant dual-engine maintenance — v1's `runner.py` alone is ~760 lines with its own loop/finally/fetch/on_max semantics — and for one maintainer that dual maintenance is the actual hubris. The one real risk is a v1 pipeline running in a sibling checkout breaking on `medulla upgrade`, but that's the contract of a version-gated 0.x tool. Clean engine > option value of dual support, here.

SUMMARY: Bet 4 is the moat, Bet 3 will hurt within a year, and a per-node visit cap is the one missing guardrail worth adding before v2 calcifies.

---

# gpt5

I ran `cd medulla/cli-agent && MEDULLA_RETRY_DELAY_S=0 pytest -q`; result: 187 passed in 22.33s, and `pytest --collect-only` reported 187 tests. (R) Discrepancies: the live-suite README and `run-all.sh` cover t1-t20, i.e. 20 live tests, not 19, and "no finally" is workflow-level only because v2 still uses Python `finally` for lock/process cleanup at `medulla/cli-agent/medulla/v2/engine.py:945-947` and `procrun.py:117-128`. (R)

A. Strongest bet: #2, the signal-contract split. (G) The validator forbids bare user routes on pool nodes at `contract.py:212-216`, `scan_stdout(..., known=None)` records pool body signals as data at `engine.py:125-148`, and pool classification deliberately erases body/post signals before join routing at `engine.py:368-379`. (R) That prevents the worst failure mode: one worker accidentally driving the graph while sibling side effects are still running. (G) The runner-up is #1 because `_run_attempts` really centralizes pre/body/post/fallback for decision and pool inputs at `engine.py:261-447`. (R)

Most likely regretted: #3, specifically "agent pool silence at rc 0 is ok." (G) The contract admits that without `post`, a pool can count air at `README.md:136`, the engine marks pool rows ok when the outcome is not `__failed__`/`__default__` at `engine.py:629-646`, and tests lock this in with a silent fake-agent pool succeeding after one attempt at `tests/test_pools.py:59-77`. (R) In real workflows, people will forget `post` on a new agent pool and get a false green, which is worse than a loud `__default__`. (G) Bet #5 has a second-order risk: opencode/agy rely on the line-start heuristic, and the code documents the residual leak where a file line that is itself a tag can still route at `harness.py:40-47`; claude/codex are safer because they filter structured assistant text only at `harness.py:125-151` and `harness.py:202-219`. (R)

B. v2 genuinely lost operator affordances. (G) v1 had transition-array runner steps, `reset_iterations`, and inline post-transition work in `_resolve_signal_target` / `_execute_steps` at old `runner.py:54-98`, documented at old `README.md:308-316`; v2 forces those into ordinary nodes. (R) v1 had `finally` and `max_iterations`/`on_max` at old `runner.py:437-458` and `runner.py:680-693`, documented at old `README.md:186-189`; v2 replaces that with budget-gate shell patterns at `README.md:295-306`. (R) v1 had loop `list` plus per-item `fetch` into `__item__` at old `runner.py:461-536`, documented at old `README.md:219-239`; v2 inputs are cleaner but make "fetch current item, route mid-loop, preserve current item for the next stage" less native. (G) v1 also had a realtime signal callback that could kill the child process after a transition signal at old `executor.py:47-60`; v2 explicitly captures the full body output and has no signal callback in `procrun.py:1-8`, so an agent that emits early but keeps working still consumes the attempt until exit/timeout. (R) Finally, v1's global `.medulla/vars.yaml` and `_loop_stage` auto-resume were ugly but hand-editable at old `runner.py:331-341` and old `state.py:6-40`; v2's immutable run snapshot and resume refusal for `--var`/`--node` at `cli.py:115-118` are safer but less hackable. (R) The old README also documented `graph`/direct pipeline mode at old `README.md:37-58`; current v2 CLI exposes validate/dry-run but no graph command at `medulla/cli-agent/medulla/v2/cli.py:84-141`. (R)

C. I would change one decision: require `post` on agent pool nodes unless the author writes an explicit opt-in such as `accept_silent: true`. (G) Shell pools can keep rc semantics, but agent pools should make "trust rc 0 with no artifact proof" visually noisy in YAML and ideally a validation decision. (G) This preserves the pool/join model while converting the easiest false-success path into either an explicit waiver or a load-time error. (G)

D. Deleting v1 outright was the right call for a single-maintainer tool with one production workflow, but only barely. (G) The v2 loader rejects non-`"2"` pipelines with a migration pointer at `contract.py:248-253`, and the new shim says v1 is gone at `medulla/cli-agent/medulla/cli.py:1-6`. (R) A compatibility layer would have preserved exactly the messy escape hatches v2 is trying to kill, and the old surface was broad: stages, loops, fetch, transition steps, graph, host bridge, and ad-hoc exit codes. (R) For a general userbase, no compatibility layer would be hubris; for this repo's single workflow and a passing 187-test suite plus live battle matrix, it is a defensible hard cut. (G)

---

# sonnet

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

---

# gemini

# Medulla v2 Engine Review

## A. Strongest and Most Regretted Bets

**Strongest Bet: The Signal-Contract Switch (Bet 2)**
The decision to treat pool bodies' signals strictly as data (recorded in the manifest) rather than routing instructions is brilliant. In v1, a loop item's output could hijack the entire graph if the agent hallucinated or a tool echoed a tag like `<signal:__next_item__>`. In v2, `engine.py` enforces `body_signal=None if pool_mode else body_scan.first_known` (R). This strict layer boundary—pool bodies produce data, joins produce transitions—makes prompt injection against the orchestrator's control flow impossible in parallel workloads.

**Most Regretted Bet: Silence Semantics Bypassing Fallback (Bet 3)**
The rule that an agent exiting 0 with silence never triggers a fallback is a severe operational flaw. According to `classify.py` (`next_move`), when a primary agent fails to emit a signal after `max_attempts`, it returns `LoopMove(Move.DONE, SIG_DEFAULT)` directly (R), bypassing `Move.SWITCH_FALLBACK`.
The README acknowledges silence as "the most common agent flake: the work happened, the tag didn't" (R). However, consistent silence is often a symptom of an alignment crisis (e.g., the model refuses to emit XML tags) or format decay. Retrying the same broken primary model will just hit the same wall. The fallback model is the exact mechanism designed to recover from primary model quirks, yet this design explicitly prohibits the fallback from saving the node.

## B. What v2 Genuinely Lost Versus v1

The tests and battle suite cannot show that v2 lost **graph-level visibility for per-item sub-workflows**. 
In v1, the `loop_state` was held in the engine's main loop (seen in v1's `runner.py` via `git show 53ec1d3` (R)). You could leave a looped stage via a non-loop signal, traverse through multiple other nodes to process the current item (using `_loop_stage` and `__list_item__`), and eventually route back to `__next_item__`. 
In v2, the presence of `inputs:` forces a scatter-gather pool execution that blocks inside a single node (`test_pools.py` proves workers are spun up and joined entirely within the node's `run()` phase (R)). If you need a multi-step workflow per item (e.g., generate -> compile -> test), you cannot represent that sequence as nodes in the graph anymore. You are forced to push that logic down into an opaque shell script (`shell: bash sequence.sh`), completely losing Medulla's observability, attempt tracking, and fallback capabilities for those inner steps (G). Furthermore, the loss of `max_iterations` means infinite loops in the graph now require boilerplate `budget-gate` shell nodes everywhere just to increment variables (R).

## C. The ONE Design Decision to Change

**I would rewrite `classify.py` to allow silent agents to trigger the fallback.**
Currently, `next_move()` forces `Verdict.SILENT` directly to `Move.DONE, SIG_DEFAULT` when attempts run out on the primary phase (R). I would change it to transition to `Move.SWITCH_FALLBACK` if a fallback exists.
*Why?* Silence at rc 0 is a failure to fulfill the orchestrator's contract. If the primary model fails mechanically (rc != 0 or timeout), it gets a fallback. If it fails communicatively (silence), it is equally dead, but the current engine artificially denies it the fallback parachute. Given that models frequently break formatting rules without crashing, this restriction makes the pipeline far more brittle than it needs to be.

## D. Verdict on Deleting v1

**Deleting v1 outright without a compatibility layer was the right call, not hubris.** (G)
The fundamental execution models are incompatible. A v1 loop spans the graph main loop; a v2 pool is an isolated parallel execution context where vars fold atomically at the end. Building a shim to map v1's `__next_item__` loop states to v2's `inputs:` scatter-gather pools would be structurally impossible without writing a second engine inside the first. For a single-maintainer tool managing one production workflow (spar), carrying a leaky, complex compatibility shim would permanently paralyze development. Pulling the band-aid off was the only survivable path.

<signal:finished>review complete</signal:finished>

---

