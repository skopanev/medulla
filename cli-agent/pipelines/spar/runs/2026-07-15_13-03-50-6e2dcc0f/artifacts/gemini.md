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
