"""The medulla engine: boot -> node loop -> finish.

One machine: every node runs through the _run_attempts seam (pre -> guard ->
body attempts primary->fallback with post per attempt); a node with inputs is
a pool of seam calls, a node without is a pool of one phantom input.
"""
from __future__ import annotations

import time
from pathlib import Path

from .errors import (
    E_DEADLINE,
    E_INTERNAL,
    E_VALIDATION,
    EngineCrash,
)
from .model import (
    EXIT_FAIL,
    EXIT_OK,
    SIG_TIMEOUT,
    TERMINALS,
    Node,
    Workflow,
)
from .rundir import RunStore

EXIT_CODE = {"succeeded": 0, "crashed": 1, "failed": 2, "timed_out": 2, "interrupted": 130}

# The contract promises: "Never quote signal syntax literally in prompts —
# describe it; the engine delivers the syntax to the agent." This is that
# delivery, appended to every agent prompt FILE (never to inherited prompt
# text, so it is stamped exactly once per written file). Found by the first
# live smoke: without it, agents cannot know the tag format -> __default__.


# Reading stdout, and the environment a body runs in — v2/engine_scan.py. Re-exported:
# the suite and several call sites have always imported them from here.
from .engine_attempts import AttemptsMixin
from .engine_pool import PoolMixin
from .logfmt import assign_signal_colours, paint  # noqa: E402
from .engine_scan import (  # noqa: E402,F401
    AttemptsOutcome,
    ScanResult,
    _input_hash,
    _parse_dotenv,
    _retry_delay,
    _sniff_inputs,
    _tail,
    _timeout_env,
    load_dotenv,
    log,
    scan_stdout,
    step_end,
    step_start,
)
from .engine_vars import VarsMixin


class Engine(VarsMixin, AttemptsMixin, PoolMixin):
    def __init__(self, workflow: Workflow, store: RunStore, workdir: Path,
                 launch_dir: Path | None = None):
        self.p = workflow
        self.store = store
        self.workdir = workdir
        self.dotenv = load_dotenv(workflow.dir, launch_dir or workdir) \
            if workflow.dir else {}
        self.vars: dict[str, str] = dict(workflow.vars)
        self.last: dict = {}
        self._in_timeout_handler = False
        self.deadline: float | None = (
            time.monotonic() + workflow.timeout if workflow.timeout else None
        )
        self.steps = 0
        self.manifests: dict[str, Path] = {}   # node -> manifest path (engine map, not vars)

    def _warn_unrouted_timeouts(self) -> None:
        """Name the nodes that set a timeout and never route the fact it produces.

        __timeout__ is offered, not imposed: a node that does not name it keeps seeing
        __failed__ exactly as before, so no workflow written earlier changes behaviour.
        The cost of that compatibility is silence — an author sets `timeout: 3600`,
        assumes a timeout is now distinguishable, and it is not.

        So the engine says it once per run and does not repeat itself per step. A
        warning and not an error on purpose: making it fatal would stop every workflow
        on this machine at once — measured, 66 nodes carry a timeout and 66 of them route
        no __timeout__, the pre-push compatibility gate among them. A rule that has to be
        pushed past its own gate to be introduced is not ready to be a rule.
        """
        # Only a wall the AUTHOR put there. Every agent body has a silence watchdog by
        # default, so testing for "can this node time out" would warn on all of them and
        # the warning would be muted within a day. The question is narrower: did somebody
        # write a deadline here and then not say where it goes.
        wall_in_defaults = self.p.defaults.timeout is not None
        for name, node in self.p.nodes.items():
            action = node.action
            has_wall = (action.timeout is not None or wall_in_defaults
                        or (action.kind == "agent"
                            and action.agent.idle_timeout is not None))
            if has_wall and SIG_TIMEOUT not in self.p.known_signals(node):
                log(paint(f"node '{name}' sets a timeout but routes no __timeout__ — "
                          f"a timeout there arrives as __failed__", "yellow"))

    # ── deadline ──
    def _remaining(self) -> float | None:
        if self.deadline is None:
            return None
        return self.deadline - time.monotonic()

    def _check_deadline(self) -> None:
        rem = self._remaining()
        if rem is not None and rem <= 0:
            raise EngineCrash(E_DEADLINE, f"workflow timeout ({self.p.timeout}s) exhausted")

    def _clamp(self, timeout_s: float) -> float:
        """Clamp a child timeout to the remaining budget (clamp, not crash)."""
        rem = self._remaining()
        if rem is None:
            return float(timeout_s)
        if rem <= 0:
            raise EngineCrash(E_DEADLINE, f"workflow timeout ({self.p.timeout}s) exhausted")
        return min(float(timeout_s), rem)

    # ── env ──
    # Linux caps a SINGLE env string at MAX_ARG_STRLEN (131072B) exactly as it caps one
    # argv string, so a var this big kills execve for EVERY body: measured in the
    # container, 130KB passed and 200KB raised "Argument list too long" from `true`.
    # Fail HERE, naming the var, instead of letting the first body die on an OSError
    # that says nothing about which value caused it.
    _MAX_ENV_VALUE = 100_000

    def _run_decision(self, node: Node, step_dir: Path, step_no: int):
        known = self._known(node)
        render_fn = lambda text, what, required=True: self._render_or_crash(
            text, node, what, required)
        outcome = self._run_attempts(
            node, step_dir, render_fn,
            apply_pre_vars=self._apply_vars,    # decision = max_parallel 1: vars apply live
            attempt_ns=f"{step_no:03d}", known=known,
            echo=self._make_echo(node),         # live operator stream (decision only:
        )                                       # pool workers would interleave into soup
        if outcome.timed_out and (rem := self._remaining()) is not None and rem <= 0:
            # killed by the exhausted RUN budget, not its own timeout: a __failed__
            # here would be exit 2 (not resumable) — this is E_DEADLINE, same law
            # as deadline-killed pool inputs
            raise EngineCrash(E_DEADLINE,
                              f"workflow timeout ({self.p.timeout}s) exhausted during "
                              f"node '{node.name}'", node=node.name)
        for u in outcome.updates:
            log(f"update: {u}")
        self._apply_vars(outcome.pending_vars)  # body/post vars: routed outcome only
        return outcome.signal, outcome.message, {
            "attempts": outcome.attempts, "rc": outcome.rc, "timed_out": outcome.timed_out,
            "fallback": outcome.fallback_used, "harness": outcome.harness,
            "model": outcome.model, "signals": outcome.signals,
        }

    # ── pool machinery ────────────────────────────────────────────────────────

    def replay(self) -> str:
        """Returns the node to continue from. The journal logs COMPLETED steps only,
        so the last row's `next` IS the interrupted/never-started node."""
        rows = self.store.read_journal()
        saved_vars = self.store.read_vars()
        if saved_vars is not None:
            self.vars = saved_vars
        current = self.p.start
        for row in rows:
            current = row.get("next", current)
            self.steps = row.get("step", self.steps)
            self.last = {"node": row.get("node", ""), "signal": row.get("signal", ""),
                         "message": row.get("message", ""), "rc": row.get("rc", "")}
            if row.get("kind") == "pool":
                mp = (self.store.steps_dir / f"{row['step']:03d}-{row['node']}" / "manifest.jsonl")
                self.manifests[row["node"]] = mp
        self.store.set_step_counter(self.steps)   # or step dirs silently collide from 001
        if current in TERMINALS:
            return current                        # finished, outcome.json missing — caller finalizes
        if current not in self.p.nodes:
            raise EngineCrash(E_VALIDATION, f"resume: unknown node '{current}' in journal")
        log(f"resume: {len(rows)} completed step(s), continuing at '{current}'")
        return current

    def synthesize_terminal(self, terminal: str) -> dict:
        """The crash window between the terminal journal row and outcome.json:
        the run DID finish — finalize from the journal instead of re-running
        (or crashing with 'nothing to resume', which this replaced)."""
        log(f"resume: journal already terminal ({terminal}) — finalizing outcome")
        if terminal == EXIT_OK:
            return {"outcome": "succeeded", "exit_code": 0}
        return {"outcome": "failed", "exit_code": 2,
                "error": {"code": "SIGNAL_FAIL", "message": self.last.get("message", ""),
                          "node": self.last.get("node", ""), "step": self.steps,
                          "signal": self.last.get("signal", "")}}

    # ── main loop ──
    def run(self, start_override: str | None = None) -> dict:
        """Run the graph. With on_timeout set, the whole-run deadline routes instead of
        crashing — see _run_graph and _run_timeout_handler."""
        try:
            return self._run_graph(start_override)
        except EngineCrash as crash:
            if crash.code != E_DEADLINE or self.p.on_timeout is None:
                raise
            if self._in_timeout_handler:
                # The handler itself ran out of time. There is no second handler and
                # there must not be: a cleanup that loops on the deadline that summoned
                # it is worse than no cleanup, because it also eats the crash report.
                log(paint(f"on_timeout node '{self.p.on_timeout}' ran out of time too",
                          "red"))
                raise
            log(paint(f"deadline reached — running on_timeout node "
                      f"'{self.p.on_timeout}'", "yellow"))
            return self._run_timeout_handler(crash)

    def _run_timeout_handler(self, crash: EngineCrash) -> dict:
        """One pass through the on_timeout node, on its OWN budget.

        The deadline is spent by definition when this is called, so a handler clamped to
        what remains is killed in the same breath it is started — which is why the run's
        deadline is lifted for it rather than extended by a grace figure nobody can pick
        correctly. Its own `timeout:` is the only wall it has, and it needs one: the
        contract's per-action default applies if the author names none.

        It runs ONCE. Whatever it routes to, the run ends after it — a handler that could
        route back into the graph would be a second run with no deadline at all.
        """
        self._in_timeout_handler = True
        self.deadline = None                  # its own timeout is the only wall left
        node = self.p.nodes[self.p.on_timeout]
        step, step_dir = self.store.new_step_dir(node.name)
        self.steps += 1
        log(step_start(step, node.name))
        t0 = time.monotonic()
        signal, message, stats = (
            self._run_pool(node, step_dir, step) if node.is_pool
            else self._run_decision(node, step_dir, step))
        duration = round(time.monotonic() - t0, 2)
        self.store.journal_append({
            "step": step, "node": node.name,
            "kind": "pool" if node.is_pool else "decision",
            "signal": signal, "next": EXIT_FAIL, "duration_s": duration,
            "message": _tail(message, 8000), "on_timeout": True,
        })
        log(step_end(step, node.name, signal, EXIT_FAIL, duration))
        # The run TIMED OUT. What the handler managed to do does not change that, and a
        # handler that returned ok must not be able to report the run as a success.
        return {"outcome": "timed_out", "exit_code": 2,
                "error": {"code": crash.code, "message": crash.message,
                          "node": crash.node, "handled_by": node.name}}

    def _run_graph(self, start_override: str | None = None) -> dict:
        current = start_override or self.p.start
        if current not in self.p.nodes:
            raise EngineCrash(E_VALIDATION, f"--node: unknown node '{current}'")
        # Hand the whole graph's vocabulary to the palette BEFORE the first step: the
        # assignment must see every signal at once, or the third one to appear cannot be
        # kept off a colour the first two already took.
        assign_signal_colours(
            {sig for n in self.p.nodes.values() for sig in self.p.known_signals(n)})
        self._warn_unrouted_timeouts()
        self.store.write_vars(self.vars)
        started = time.monotonic()

        while True:
            node = self.p.nodes[current]
            self._check_deadline()   # raises E_DEADLINE; caught below when on_timeout is set
            self.steps += 1
            step, step_dir = self.store.new_step_dir(node.name)
            log(step_start(step, node.name))
            t0 = time.monotonic()

            if node.is_pool:
                signal_name, message, stats = self._run_pool(node, step_dir, step)
                journal_kind = "pool"
            else:
                signal_name, message, stats = self._run_decision(node, step_dir, step)
                journal_kind = "decision"

            target = self.p.resolve_route(node, signal_name)
            if target is None:
                raise EngineCrash(E_INTERNAL, f"no route for signal '{signal_name}'",
                                  node=node.name)

            duration = round(time.monotonic() - t0, 2)
            self.last = {"node": node.name, "signal": signal_name, "message": message,
                         "rc": stats.get("rc", "")}
            journal_row = {"step": step, "node": node.name, "kind": journal_kind,
                           "signal": signal_name, "next": target, "duration_s": duration,
                           # resume rebuilds last.message from this; 400 bytes broke
                           # payload-carrying {{last.message}} templates (audit G8)
                           "message": _tail(message, 8000)}
            if journal_kind == "pool":
                journal_row.update({k: stats.get(k) for k in
                                    ("inputs_total", "inputs_ok", "min_success")})
            else:
                journal_row.update({
                    "attempts": stats.get("attempts"), "rc": stats.get("rc"),
                    "timed_out": stats.get("timed_out"), "fallback": stats.get("fallback"),
                    "harness": stats.get("harness"), "model": stats.get("model"),
                    # owner decision: EVERY signal the node produced (update/var/bare,
                    # stdout order, concluding path) — replay ignores the field
                    "signals": stats.get("signals") or [],
                })
            self.store.journal_append(journal_row)
            log(step_end(step, node.name, signal_name, target, duration))

            if target in TERMINALS:
                total = round(time.monotonic() - started, 2)
                if target == EXIT_OK:
                    return {"outcome": "succeeded", "exit_code": 0,
                            "steps": self.steps, "duration_s": total,
                            "run_id": self.store.run_id}
                return {
                    "outcome": "failed", "exit_code": 2,
                    "error": {"code": "SIGNAL_FAIL", "message": message,
                              "node": node.name, "step": step, "signal": signal_name},
                    "steps": self.steps, "duration_s": total, "run_id": self.store.run_id,
                }
            current = target



# Running a workflow end to end — v2/engine_run.py. Imported at the BOTTOM and inside
# the module that needs it: engine_run imports Engine from here, so a top-level import
# either way closes a cycle.
from .engine_run import (  # noqa: E402,F401
    _normalize_outcome,
    find_resumable,
    run_workflow,
)
