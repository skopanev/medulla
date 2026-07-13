"""The v2 engine: boot -> node loop -> finish. Walking-skeleton scope (part 1):
decision nodes with shell bodies, full classification, routing, journal, outcome.
Pools land in part 3; agent adapters in part 5; pre/post hooks in part 2.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

from ..signals import extract_signals  # v1 utility, pure text extraction — kept
from .classify import Move, Verdict, classify_attempt, next_move
from .contract import load_pipeline
from .errors import (
    EngineCrash, E_DEADLINE, E_HARNESS, E_INTERNAL, E_RENDER, E_VALIDATION,
)
from .model import (
    CHANNEL_SIGNALS, EXIT_FAIL, EXIT_OK, Pipeline, Node, Action,
    SIG_DEFAULT, SIG_FAILED, TERMINALS,
)
from .procrun import run as proc_run
from .render import RenderError, render
from .rundir import RunStore

EXIT_CODE = {"succeeded": 0, "crashed": 1, "failed": 2, "interrupted": 130}


def log(msg: str) -> None:
    print(f"[medulla] {msg}", file=sys.stderr)


def _tail(text: str, n: int = 400) -> str:
    text = text.strip()
    return text[-n:] if len(text) > n else text


class Engine:
    def __init__(self, pipeline: Pipeline, store: RunStore, workdir: Path):
        self.p = pipeline
        self.store = store
        self.workdir = workdir
        self.vars: dict[str, str] = dict(pipeline.vars)
        self.last: dict = {}          # node/signal/message/rc of the previous node
        self.deadline: float | None = (
            time.monotonic() + pipeline.timeout if pipeline.timeout else None
        )
        self.steps = 0

    # ── deadline ──
    def _remaining(self) -> float | None:
        if self.deadline is None:
            return None
        return self.deadline - time.monotonic()

    def _check_deadline(self) -> None:
        rem = self._remaining()
        if rem is not None and rem <= 2:
            raise EngineCrash(E_DEADLINE, f"pipeline timeout ({self.p.timeout}s) exhausted")

    def _clamp(self, timeout_s: int) -> float:
        rem = self._remaining()
        if rem is None:
            return float(timeout_s)
        if rem <= 2:
            raise EngineCrash(E_DEADLINE, f"pipeline timeout ({self.p.timeout}s) exhausted")
        return min(float(timeout_s), rem)

    # ── env ──
    def _base_env(self) -> dict[str, str]:
        env = dict(self.vars)
        env["MEDULLA_RUN_ID"] = self.store.run_id
        env["MEDULLA_RUN_DIR"] = str(self.store.dir)
        if self.last:
            env["MEDULLA_LAST_NODE"] = str(self.last.get("node", ""))
            env["MEDULLA_LAST_SIGNAL"] = str(self.last.get("signal", ""))
            env["MEDULLA_LAST_MESSAGE"] = str(self.last.get("message", ""))
            env["MEDULLA_LAST_RC"] = str(self.last.get("rc", ""))
        return env

    # ── signals from captured stdout (stdout only — stderr never routes) ──
    def _scan_stdout(self, stdout: str, node: Node, apply_state: bool):
        """Returns (first_known_signal, its_body). Channel signals: var applies only
        when apply_state=True (concluding attempt, fold law); update logs on that
        pass only, so the peek scan doesn't duplicate lines."""
        known = self.p.known_signals(node) - set(CHANNEL_SIGNALS)
        first_sig, first_body = None, ""
        pending_vars: dict[str, str] = {}
        for name, attrs, body in extract_signals(stdout):
            if name == "update":
                if apply_state:
                    log(f"update: {body}")
                continue
            if name == "var":
                key = (attrs or {}).get("key", "")
                if key and body:
                    pending_vars[key] = body
                continue
            if name in known and first_sig is None:
                first_sig, first_body = name, body
        if apply_state and pending_vars:
            from .contract import VAR_NAME_RE
            from .model import ENV_BLACKLIST_EXACT, ENV_BLACKLIST_PREFIX
            for key, value in pending_vars.items():
                if not VAR_NAME_RE.match(key) or key in ENV_BLACKLIST_EXACT or \
                        any(key.startswith(p) for p in ENV_BLACKLIST_PREFIX):
                    log(f"warn: var '{key}' rejected (reserved/invalid name)")
                    continue
                self.vars[key] = value
            self.store.write_vars(self.vars)
        return first_sig, first_body

    # ── decision node execution ──
    def _run_decision(self, node: Node, step_dir: Path) -> tuple[str, str, dict]:
        """Returns (outcome_signal, message, stats)."""
        action = node.action
        if action.kind != "shell":
            raise EngineCrash(
                E_HARNESS,
                "agent adapters land in part 5 of the build — use shell bodies for now",
                node=node.name,
            )

        # render once per node run; retries reuse the rendered text
        try:
            rendered = render(action.shell, self.p.dir, self.vars, last=self.last)
        except RenderError as exc:
            raise EngineCrash(E_RENDER, str(exc), node=node.name)
        (step_dir / "body.sh").write_text(rendered, encoding="utf-8")

        max_attempts = self.p.action_max_attempts(action)
        ignore_rc = self.p.action_ignore_exit_code(action)
        timeout_s = self.p.action_timeout(action)
        # shell actions have no fallback (validator guarantees), phase is always primary
        attempt = 0
        total_attempts = 0
        result = None
        while True:
            attempt += 1
            total_attempts += 1
            self._check_deadline()
            log_path = step_dir / f"attempt-{total_attempts}-shell.txt"
            result = proc_run(
                rendered, self.workdir, self._clamp(timeout_s),
                extra_env=self._base_env(), log_path=log_path,
            )
            # signals from a not-yet-final attempt must not mutate state:
            # peek first, apply vars only if this attempt concludes the node
            sig, body = self._scan_stdout(result.stdout, node, apply_state=False)
            decision = classify_attempt(
                kind="shell", rc=result.rc, timed_out=result.timed_out,
                body_signal=sig, post_rc=None, post_signal=None,
                ignore_exit_code=ignore_rc,
            )
            move = next_move(
                decision, kind="shell", phase="primary",
                attempt=attempt, max_attempts=max_attempts, has_fallback=False,
            )
            if move.move is Move.RETRY_SAME:
                log(f"attempt {attempt}/{max_attempts} failed (rc={result.rc}), retrying")
                continue
            # final: apply state signals from the concluding attempt (fold law)
            if decision.verdict in (Verdict.ROUTE, Verdict.SILENT):
                sig, body = self._scan_stdout(result.stdout, node, apply_state=True)
            outcome = move.signal
            if outcome == SIG_FAILED:
                message = f"body died: rc={result.rc}, {total_attempts} attempt(s); stderr: {_tail(result.stderr)}"
            elif outcome == SIG_DEFAULT:
                message = f"no known signal emitted; stdout: {_tail(result.stdout)}"
            else:
                message = body
            stats = {"attempts": total_attempts, "rc": result.rc, "timed_out": result.timed_out}
            return outcome, message, stats

    # ── main loop ──
    def run(self, start_override: str | None = None) -> dict:
        current = start_override or self.p.start
        if current not in self.p.nodes:
            raise EngineCrash(E_VALIDATION, f"--node: unknown node '{current}'")
        self.store.write_vars(self.vars)
        started = time.monotonic()

        while True:
            node = self.p.nodes[current]
            self._check_deadline()
            self.steps += 1
            step, step_dir = self.store.new_step_dir(node.name)
            log(f"step {step} | {node.name}")
            t0 = time.monotonic()

            if node.is_pool:
                raise EngineCrash(E_INTERNAL, "pool nodes land in part 3 of the build", node=node.name)

            signal_name, message, stats = self._run_decision(node, step_dir)

            target = self.p.resolve_route(node, signal_name)
            if target is None:
                # a user signal without any route cannot conclude a node:
                # classification guarantees ROUTE only for known signals, so this
                # is unreachable — guard for engine bugs.
                raise EngineCrash(E_INTERNAL, f"no route for signal '{signal_name}'", node=node.name)

            duration = round(time.monotonic() - t0, 2)
            self.last = {"node": node.name, "signal": signal_name, "message": message,
                         "rc": stats.get("rc", "")}
            self.store.journal_append({
                "step": step, "node": node.name, "kind": "decision",
                "attempts": stats.get("attempts"), "rc": stats.get("rc"),
                "timed_out": stats.get("timed_out"), "signal": signal_name,
                "next": target, "duration_s": duration,
            })
            log(f"step {step} | {node.name} -> {signal_name} -> {target} ({duration}s)")

            if target in TERMINALS:
                total = round(time.monotonic() - started, 2)
                if target == EXIT_OK:
                    return {"outcome": "succeeded", "exit_code": 0,
                            "steps": self.steps, "duration_s": total, "run_id": self.store.run_id}
                return {
                    "outcome": "failed", "exit_code": 2,
                    "error": {"code": "SIGNAL_FAIL", "message": message,
                              "node": node.name, "step": step, "signal": signal_name},
                    "steps": self.steps, "duration_s": total, "run_id": self.store.run_id,
                }
            current = target


def run_pipeline(
    pipeline_path: Path,
    cli_vars: dict[str, str] | None = None,
    start_override: str | None = None,
    workdir: Path | None = None,
) -> int:
    """Load, run, write outcome.json, return the process exit code (0/1/2/130)."""
    workdir = workdir or Path.cwd()
    store = None
    try:
        pipeline = load_pipeline(Path(pipeline_path))
        if cli_vars:
            from .contract import _validate_var_name
            for k in cli_vars:
                _validate_var_name(k, "--var")
            pipeline.vars.update({k: str(v) for k, v in cli_vars.items()})
        store = RunStore.create(pipeline.dir, pipeline.path.read_text(encoding="utf-8"))
        log(f"run {store.run_id} -> {store.dir}")
        engine = Engine(pipeline, store, workdir)
        outcome = engine.run(start_override)
        store.write_outcome(outcome)
        return outcome["exit_code"]
    except EngineCrash as crash:
        outcome = {
            "outcome": "crashed", "exit_code": 1,
            "error": {"code": crash.code, "message": crash.message, "node": crash.node},
        }
        log(f"crash {crash.code}: {crash.message}")
        if store is not None:
            store.write_outcome(outcome)
        return 1
    except KeyboardInterrupt:
        if store is not None:
            store.write_outcome({"outcome": "interrupted", "exit_code": 130})
        return 130
