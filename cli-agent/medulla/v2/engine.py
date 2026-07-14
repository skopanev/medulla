"""The v2 engine: boot -> node loop -> finish.

Part-2 scope: decision nodes with shell AND agent (fake harness) bodies, pre/post
hook execution, full attempts+fallback via the _run_attempts seam (pools in part 3
reuse it with their own render_fn). Pools land in part 3; real adapters in part 5.
"""
from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..signals import extract_signals  # v1 utility, pure text extraction — kept
from .classify import Move, Verdict, classify_attempt, next_move
from .contract import load_pipeline, VAR_NAME_RE
from .errors import (
    EngineCrash, E_DEADLINE, E_INTERNAL, E_RENDER, E_VALIDATION,
)
from .harness import resolve as resolve_harness
from .model import (
    Action, CHANNEL_SIGNALS, ENGINE_FACTS, ENV_BLACKLIST_EXACT, ENV_BLACKLIST_PREFIX,
    EXIT_FAIL, EXIT_OK, HOOK_TIMEOUT_S, Node, Pipeline,
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


# ── structured signal scan (foundation for pool manifests) ──────────────────

@dataclass
class ScanResult:
    first_known: str | None = None
    first_body: str = ""
    vars: dict[str, str] = field(default_factory=dict)
    updates: list[str] = field(default_factory=list)


def scan_stdout(stdout: str, known: set[str]) -> ScanResult:
    """stdout only — stderr never routes. Engine facts are excluded from `known`
    upstream: a body printing <signal:__failed__> must never route (namespace law)."""
    res = ScanResult()
    for name, attrs, body in extract_signals(stdout):
        if name == "update":
            res.updates.append(body)
            continue
        if name == "var":
            key = (attrs or {}).get("key", "")
            if key and body:
                res.vars[key] = body
            continue
        if name in known and res.first_known is None:
            res.first_known, res.first_body = name, body
    return res


@dataclass
class AttemptsOutcome:
    signal: str                      # user signal | __failed__ | __default__
    message: str
    attempts: int                    # total executions across phases (0 = pre decided)
    attempts_primary: int = 0
    attempts_fallback: int = 0
    rc: int | None = None
    timed_out: bool = False
    fallback_used: bool = False
    concluding_phase: str | None = None   # "primary" | "fallback" | None (pre decided)
    harness: str | None = None            # harness that produced the concluding outcome
    model: str | None = None
    guarded: bool = False                 # pre emitted a routing signal; body never ran
    pending_vars: dict[str, str] = field(default_factory=dict)  # fold law: caller applies
    updates: list[str] = field(default_factory=list)


class Engine:
    def __init__(self, pipeline: Pipeline, store: RunStore, workdir: Path):
        self.p = pipeline
        self.store = store
        self.workdir = workdir
        self.vars: dict[str, str] = dict(pipeline.vars)
        self.last: dict = {}
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
        if rem is not None and rem <= 0:
            raise EngineCrash(E_DEADLINE, f"pipeline timeout ({self.p.timeout}s) exhausted")

    def _clamp(self, timeout_s: float) -> float:
        """Clamp a child timeout to the remaining budget (clamp, not crash)."""
        rem = self._remaining()
        if rem is None:
            return float(timeout_s)
        if rem <= 0:
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
            env["MEDULLA_LAST_EVENT_JSON"] = json.dumps(self.last, ensure_ascii=False)
        return env

    # ── vars (fold law application point) ──
    def _apply_vars(self, pending: dict[str, str]) -> None:
        if not pending:
            return
        for key, value in pending.items():
            if not VAR_NAME_RE.match(key) or key in ENV_BLACKLIST_EXACT or \
                    any(key.startswith(p) for p in ENV_BLACKLIST_PREFIX):
                log(f"warn: var '{key}' rejected (reserved/invalid name)")
                continue
            self.vars[key] = value
        self.store.write_vars(self.vars)

    # ── render helpers ──
    def _known(self, node: Node) -> set[str]:
        return self.p.known_signals(node) - set(CHANNEL_SIGNALS) - set(ENGINE_FACTS)

    def _render_or_crash(self, text: str, node: Node, what: str) -> str:
        """Decision-context render: any breakage is a pipeline bug -> E_RENDER.
        Part-3 pools pass their own render_fn with fail-the-input semantics."""
        try:
            rendered = render(text, self.p.dir, self.vars, last=self.last)
        except RenderError as exc:
            raise EngineCrash(E_RENDER, f"{what}: {exc}", node=node.name)
        if not rendered.strip():
            raise EngineCrash(E_RENDER, f"{what} rendered empty (broken template or empty field)",
                              node=node.name)
        return rendered

    # ── the attempts seam ────────────────────────────────────────────────────
    # Owns the FULL hook machinery (panel: pre and post live INSIDE the seam so
    # part-3 pool workers get identical semantics by calling this per input):
    #   pre -> [guard?] -> body attempts (primary -> fallback) with post per attempt.
    # The seam never mutates engine state: `apply_pre_vars` is the caller's policy
    # (decision: apply to self.vars so the body render sees them; pool at
    # max_parallel>1: record to manifest + local ctx), and body/post vars come
    # back as pending_vars for the caller to apply per the fold law.
    def _run_attempts(
        self,
        node: Node,
        step_dir: Path,
        render_fn,
        apply_pre_vars,
        attempt_ns: str,
        known: set[str],
    ) -> AttemptsOutcome:
        action = node.action

        if node.pre is not None:
            pre_rendered = render_fn(node.pre, "pre")
            hook_timeout = self._clamp(HOOK_TIMEOUT_S)
            pre_env = {**self._base_env(),
                       "MEDULLA_TIMEOUT_S": str(int(hook_timeout)),
                       "MEDULLA_HARNESS": "pre"}
            pre_res = proc_run(pre_rendered, self.workdir, hook_timeout,
                               extra_env=pre_env, log_path=step_dir / "pre.txt")
            pre_scan = scan_stdout(pre_res.stdout, known)
            if pre_res.rc != 0:
                return AttemptsOutcome(
                    signal=SIG_FAILED,
                    message=f"pre hook failed: rc={pre_res.rc}; stderr: {_tail(pre_res.stderr)}",
                    attempts=0, rc=pre_res.rc, timed_out=pre_res.timed_out,
                    updates=pre_scan.updates,
                )
            apply_pre_vars(pre_scan.vars)   # env prep BEFORE the body renders
            if pre_scan.first_known is not None:
                return AttemptsOutcome(       # guard: body and post are skipped
                    signal=pre_scan.first_known, message=pre_scan.first_body,
                    attempts=0, rc=0, guarded=True, updates=pre_scan.updates,
                )

        post_rendered = render_fn(node.post, "post") if node.post else None

        current = action
        phase = "primary"
        fallback = self.p.action_fallback(action) if action.kind == "agent" else None
        fallback_used = False
        harness_name: str | None = None

        body_cmd, prompt_text = self._prepare_body(current, node, step_dir, render_fn, phase)
        if current.kind == "agent":
            harness_name = current.agent.harness

        attempt = 0
        total = 0
        n_primary = 0
        n_fallback = 0
        while True:
            attempt += 1
            total += 1
            if phase == "primary":
                n_primary += 1
            else:
                n_fallback += 1
            self._check_deadline()
            eff = self._clamp(self.p.action_timeout(current))
            attempt_id = f"{attempt_ns}.{phase[0]}{attempt}"
            tag = "shell" if current.kind == "shell" else current.agent.harness
            env = {**self._base_env(),
                   "MEDULLA_TIMEOUT_S": str(int(eff)),
                   "MEDULLA_ATTEMPT_ID": attempt_id,
                   "MEDULLA_HARNESS": tag}

            result = proc_run(body_cmd, self.workdir, eff, extra_env=env,
                              log_path=step_dir / f"attempt-{total}-{tag}.txt")

            raw_text = result.stdout
            if current.kind == "agent":
                raw_text = resolve_harness(current.agent).filter_stdout(raw_text)
            body_scan = scan_stdout(raw_text, known)

            post_rc = post_signal = None
            post_scan = ScanResult()
            if post_rendered is not None:
                post_env = {**env,
                            "MEDULLA_BODY_RC": str(result.rc),
                            "MEDULLA_BODY_SIGNAL": body_scan.first_known or ""}
                post_res = proc_run(post_rendered, self.workdir,
                                    self._clamp(HOOK_TIMEOUT_S), extra_env=post_env,
                                    log_path=step_dir / f"post-{total}.txt")
                post_scan = scan_stdout(post_res.stdout, known)
                post_rc, post_signal = post_res.rc, post_scan.first_known

            decision = classify_attempt(
                kind=current.kind, rc=result.rc, timed_out=result.timed_out,
                body_signal=body_scan.first_known, post_rc=post_rc,
                post_signal=post_signal,
                ignore_exit_code=self.p.action_ignore_exit_code(current),
            )
            move = next_move(
                decision, kind=current.kind, phase=phase, attempt=attempt,
                max_attempts=self.p.action_max_attempts(current),
                has_fallback=fallback is not None,
            )

            if move.move is Move.RETRY_SAME:
                log(f"attempt {attempt_id} failed (rc={result.rc}), retrying")
                continue
            if move.move is Move.SWITCH_FALLBACK:
                log(f"attempt {attempt_id} failed (rc={result.rc}), switching to fallback")
                current = fallback
                phase = "fallback"
                attempt = 0
                fallback_used = True
                harness_name = current.agent.harness
                body_cmd, prompt_text = self._prepare_body(
                    current, node, step_dir, render_fn, phase, inherited_prompt=prompt_text)
                continue

            # DONE — collect state from the concluding attempt only (fold law)
            pending: dict[str, str] = {}
            updates = body_scan.updates + post_scan.updates
            if decision.verdict in (Verdict.ROUTE, Verdict.SILENT):
                pending = {**body_scan.vars, **post_scan.vars}   # post wins on conflict

            signal = move.signal
            if signal == SIG_FAILED:
                message = (f"body died: rc={result.rc}, {total} attempt(s)"
                           f"{' (fallback tried)' if fallback_used else ''}; "
                           f"stderr: {_tail(result.stderr)}")
            elif signal == SIG_DEFAULT:
                message = f"no known signal emitted; stdout: {_tail(result.stdout)}"
            elif post_signal is not None and signal == post_signal:
                message = post_scan.first_body
            else:
                message = body_scan.first_body
            return AttemptsOutcome(
                signal=signal, message=message, attempts=total,
                attempts_primary=n_primary, attempts_fallback=n_fallback,
                rc=result.rc, timed_out=result.timed_out,
                fallback_used=fallback_used, concluding_phase=phase,
                harness=harness_name,
                model=current.agent.model if current.kind == "agent" else None,
                pending_vars=pending, updates=updates,
            )

    def _prepare_body(self, action: Action, node: Node, step_dir: Path,
                      render_fn, phase: str, inherited_prompt: str | None = None):
        """Returns (command_for_procrun, rendered_prompt_text_or_None)."""
        if action.kind == "shell":
            return render_fn(action.shell, "shell"), None
        adapter = resolve_harness(action.agent)
        if action.prompt is not None:
            prompt_text = render_fn(action.prompt, "prompt")
        elif inherited_prompt is not None:
            prompt_text = inherited_prompt      # fallback reuses the primary's rendered prompt
        else:
            raise EngineCrash(E_RENDER, "agent action has no prompt", node=node.name)
        prompt_file = step_dir / ("prompt.md" if phase == "primary" else "prompt-fallback.md")
        prompt_file.write_text(prompt_text, encoding="utf-8")
        return adapter.build_argv(action.agent, prompt_file), prompt_text

    # ── decision node: the seam + decision-node policy (fold law application) ──
    def _run_decision(self, node: Node, step_dir: Path, step_no: int):
        known = self._known(node)
        render_fn = lambda text, what: self._render_or_crash(text, node, what)
        outcome = self._run_attempts(
            node, step_dir, render_fn,
            apply_pre_vars=self._apply_vars,    # decision = max_parallel 1: vars apply live
            attempt_ns=f"{step_no:03d}", known=known,
        )
        for u in outcome.updates:
            log(f"update: {u}")
        self._apply_vars(outcome.pending_vars)  # body/post vars: concluding attempt only
        return outcome.signal, outcome.message, {
            "attempts": outcome.attempts, "rc": outcome.rc, "timed_out": outcome.timed_out,
            "fallback": outcome.fallback_used, "harness": outcome.harness,
        }

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
                raise EngineCrash(E_INTERNAL, "pool nodes land in part 3 of the build",
                                  node=node.name)

            signal_name, message, stats = self._run_decision(node, step_dir, step)

            target = self.p.resolve_route(node, signal_name)
            if target is None:
                raise EngineCrash(E_INTERNAL, f"no route for signal '{signal_name}'",
                                  node=node.name)

            duration = round(time.monotonic() - t0, 2)
            self.last = {"node": node.name, "signal": signal_name, "message": message,
                         "rc": stats.get("rc", "")}
            self.store.journal_append({
                "step": step, "node": node.name, "kind": "decision",
                "attempts": stats.get("attempts"), "rc": stats.get("rc"),
                "timed_out": stats.get("timed_out"), "fallback": stats.get("fallback"),
                "harness": stats.get("harness"), "signal": signal_name,
                "next": target, "duration_s": duration,
            })
            log(f"step {step} | {node.name} -> {signal_name} -> {target} ({duration}s)")

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
