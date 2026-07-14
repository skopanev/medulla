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
    EngineCrash, E_DEADLINE, E_INPUTS, E_INPUTS_LIMIT, E_INTERNAL, E_RENDER, E_VALIDATION,
)
from .harness import resolve as resolve_harness
from .model import (
    Action, CHANNEL_SIGNALS, ENGINE_FACTS, ENV_BLACKLIST_EXACT, ENV_BLACKLIST_PREFIX,
    EXIT_FAIL, EXIT_OK, HOOK_TIMEOUT_S, INPUTS_HARD_CAP, Node, Pipeline,
    SIG_DEFAULT, SIG_DONE, SIG_EMPTY, SIG_FAILED, TERMINALS,
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


def _timeout_env(seconds: float) -> str:
    """Env representation of a clamped timeout: never "0" for a live budget —
    an agent CLI sizing its own timeout from this must not read "no limit"."""
    return str(max(1, int(round(seconds))))


def _input_hash(value) -> str:
    """Stable input identity for resume/idempotency. Python's hash() is salted."""
    import hashlib
    canonical = json.dumps(value, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _sniff_inputs(stdout: str, node_name: str) -> list:
    """First non-blank byte decides: '[' JSON array, '{' JSON-lines, else plain lines."""
    text = stdout.strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise EngineCrash(E_INPUTS, f"inputs source: broken JSON array: {exc}",
                              node=node_name)
        if not isinstance(data, list):
            raise EngineCrash(E_INPUTS, "inputs source: JSON is not an array", node=node_name)
        return data
    if text.startswith("{"):
        rows = []
        for n, line in enumerate(text.splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise EngineCrash(E_INPUTS, f"inputs source: broken JSON on line {n}: {exc}",
                                  node=node_name)
        return rows
    return [line.strip() for line in text.splitlines() if line.strip()]


# ── structured signal scan (foundation for pool manifests) ──────────────────

@dataclass
class ScanResult:
    first_known: str | None = None
    first_body: str = ""
    vars: dict[str, str] = field(default_factory=dict)
    updates: list[str] = field(default_factory=list)


def scan_stdout(stdout: str, known: set[str] | None) -> ScanResult:
    """stdout only — stderr never routes. Engine facts are excluded from `known`
    upstream: a body printing <signal:__failed__> must never route (namespace law).

    known=None is pool mode: record the first ANY bare user signal (pool routing
    tables hold only dunders, yet body signals must reach the manifest)."""
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
        if res.first_known is not None:
            continue
        if known is None:
            if name not in CHANNEL_SIGNALS and not name.startswith("__"):
                res.first_known, res.first_body = name, body
        elif name in known:
            res.first_known, res.first_body = name, body
    return res


@dataclass
class AttemptsOutcome:
    signal: str | None               # user signal | __failed__ | __default__ | None (pool silent ok)
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
    failure_class: str | None = None      # for __failed__: "pre" | "rc" | "timeout" | "post"
    recorded_signal: str | None = None    # pool: first bare signal seen (data, never outcome)
    recorded_body: str = ""
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
        self.manifests: dict[str, Path] = {}   # node -> manifest path (engine map, not vars)

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
    def _base_env(self, vars_map: dict[str, str] | None = None) -> dict[str, str]:
        env = dict(self.vars if vars_map is None else vars_map)
        env["MEDULLA_RUN_ID"] = self.store.run_id
        env["MEDULLA_RUN_DIR"] = str(self.store.dir)
        for node_name, path in self.manifests.items():
            env[f"MEDULLA_MANIFEST_{node_name.upper().replace('-', '_')}"] = str(path)
        if self.last:
            env["MEDULLA_LAST_NODE"] = str(self.last.get("node", ""))
            env["MEDULLA_LAST_SIGNAL"] = str(self.last.get("signal", ""))
            env["MEDULLA_LAST_MESSAGE"] = str(self.last.get("message", ""))
            env["MEDULLA_LAST_RC"] = str(self.last.get("rc", ""))
            env["MEDULLA_LAST_EVENT_JSON"] = json.dumps(self.last, ensure_ascii=False)
        return env

    # ── vars (fold law application point) ──
    @staticmethod
    def _valid_var_key(key: str) -> bool:
        return bool(VAR_NAME_RE.match(key)) and key not in ENV_BLACKLIST_EXACT and \
            not any(key.startswith(p) for p in ENV_BLACKLIST_PREFIX)

    def _apply_vars(self, pending: dict[str, str]) -> None:
        if not pending:
            return
        for key, value in pending.items():
            if not self._valid_var_key(key):
                log(f"warn: var '{key}' rejected (reserved/invalid name)")
                continue
            self.vars[key] = value
        self.store.write_vars(self.vars)

    # ── render helpers ──
    def _known(self, node: Node) -> set[str]:
        return self.p.known_signals(node) - set(CHANNEL_SIGNALS) - set(ENGINE_FACTS)

    def _render_or_crash(self, text: str, node: Node, what: str, required: bool = True) -> str:
        """Decision-context render: any breakage is a pipeline bug -> E_RENDER.
        required=False: an optional field rendering empty counts as absent (contract).
        Part-3 pools pass their own render_fn with fail-the-input semantics."""
        try:
            rendered = render(text, self.p.dir, self.vars, last=self.last)
        except RenderError as exc:
            raise EngineCrash(E_RENDER, f"{what}: {exc}", node=node.name)
        if required and not rendered.strip():
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
        known: set[str] | None,
        env_fn=None,
        pool_mode: bool = False,
    ) -> AttemptsOutcome:
        # env_fn: callable -> dict, the base env for hooks and bodies. Decision nodes
        # default to self._base_env (pre vars land in self.vars and are picked up on
        # the next call); part-3 pool workers pass their own (base + input ctx + local
        # pre-vars overlay) so parallel inputs never touch shared engine state.
        if env_fn is None:
            env_fn = self._base_env
        action = node.action

        pre_updates: list[str] = []
        primary_tag = "shell" if action.kind == "shell" else action.agent.harness
        if node.pre is not None:
            pre_rendered = render_fn(node.pre, "pre")
            hook_timeout = self._clamp(HOOK_TIMEOUT_S)
            pre_env = {**env_fn(),
                       "MEDULLA_TIMEOUT_S": _timeout_env(hook_timeout),
                       "MEDULLA_HARNESS": primary_tag}
            pre_res = proc_run(pre_rendered, self.workdir, hook_timeout,
                               extra_env=pre_env, log_path=step_dir / "pre.txt")
            pre_scan = scan_stdout(pre_res.stdout, known)
            pre_updates = pre_scan.updates
            # a known signal wins over rc — same grammar as everywhere else
            if pre_scan.first_known is not None:
                apply_pre_vars(pre_scan.vars)     # env prep applies before the guard routes
                return AttemptsOutcome(           # guard: body and post are skipped
                    signal=pre_scan.first_known, message=pre_scan.first_body,
                    attempts=0, rc=pre_res.rc, guarded=True, updates=pre_updates,
                )
            if pre_res.rc != 0:
                return AttemptsOutcome(
                    signal=SIG_FAILED,
                    message=f"pre hook failed: rc={pre_res.rc}; stderr: {_tail(pre_res.stderr)}",
                    attempts=0, rc=pre_res.rc, timed_out=pre_res.timed_out,
                    failure_class="pre", updates=pre_updates,
                )
            apply_pre_vars(pre_scan.vars)   # env prep BEFORE the body renders

        post_rendered = render_fn(node.post, "post") if node.post else None

        current = action
        phase = "primary"
        fallback = self.p.action_fallback(action) if action.kind == "agent" else None
        fallback_used = False
        # contract: "primary gets N, then fallback gets N" — a fallback without its
        # own max_attempts inherits the primary's effective budget
        phase_budget = self.p.action_max_attempts(action)

        body_cmd, prompt_text, agent_spec = self._prepare_body(
            current, node, step_dir, render_fn, phase)
        harness_name = agent_spec.harness if agent_spec else None

        attempt = 0
        total = 0
        n_primary = 0
        n_fallback = 0
        last_failure_class: str | None = None
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
            tag = "shell" if current.kind == "shell" else agent_spec.harness
            env = {**env_fn(),
                   "MEDULLA_TIMEOUT_S": _timeout_env(eff),
                   "MEDULLA_ATTEMPT_ID": attempt_id,
                   "MEDULLA_HARNESS": tag}

            result = proc_run(body_cmd, self.workdir, eff, extra_env=env,
                              log_path=step_dir / f"attempt-{total}-{tag}.txt")

            raw_text = result.stdout
            if current.kind == "agent":
                raw_text = resolve_harness(agent_spec).filter_stdout(raw_text)
            body_scan = scan_stdout(raw_text, known)

            post_rc = post_signal = None
            post_scan = ScanResult()
            if post_rendered is not None:
                hook_timeout = self._clamp(HOOK_TIMEOUT_S)
                post_env = {**env,
                            "MEDULLA_TIMEOUT_S": _timeout_env(hook_timeout),
                            "MEDULLA_BODY_RC": str(result.rc),
                            "MEDULLA_BODY_SIGNAL": body_scan.first_known or ""}
                post_res = proc_run(post_rendered, self.workdir,
                                    hook_timeout, extra_env=post_env,
                                    log_path=step_dir / f"post-{total}.txt")
                post_scan = scan_stdout(post_res.stdout, known)
                post_rc, post_signal = post_res.rc, post_scan.first_known

            # Pool conjunction law: ok = rc==0 AND no timeout AND no post veto.
            # Signals are DATA in pools — they are recorded, they never classify
            # ("echo <signal:x>; exit 7" must not become ok). ignore_exit_code
            # never reaches pools, not even via defaults (min_success owns that).
            decision = classify_attempt(
                kind=current.kind, rc=result.rc, timed_out=result.timed_out,
                body_signal=None if pool_mode else body_scan.first_known,
                post_rc=post_rc,
                post_signal=None if pool_mode else post_signal,
                ignore_exit_code=(False if pool_mode
                                  else self.p.action_ignore_exit_code(current)),
            )
            move = next_move(
                decision, kind=current.kind, phase=phase, attempt=attempt,
                max_attempts=phase_budget,
                has_fallback=fallback is not None,
                pool_mode=pool_mode,
            )
            if decision.failure_class is not None:
                last_failure_class = decision.failure_class

            if move.move is Move.RETRY_SAME:
                log(f"attempt {attempt_id} failed (rc={result.rc}), retrying")
                continue
            if move.move is Move.SWITCH_FALLBACK:
                log(f"attempt {attempt_id} failed (rc={result.rc}), switching to fallback")
                current = fallback
                phase = "fallback"
                attempt = 0
                fallback_used = True
                if fallback.max_attempts is not None:
                    phase_budget = fallback.max_attempts
                body_cmd, prompt_text, agent_spec = self._prepare_body(
                    current, node, step_dir, render_fn, phase, inherited_prompt=prompt_text)
                harness_name = agent_spec.harness if agent_spec else None
                continue

            # DONE — state signals are collected from a successful outcome only:
            # a ROUTED signal, or a pool's silent-ok (its vars ARE successful vars —
            # dropping them would make row.vars lie). A __default__ conclusion is a
            # communication failure: its vars must not leak (fold law).
            pending: dict[str, str] = {}
            updates = pre_updates + body_scan.updates + post_scan.updates
            silent_ok = pool_mode and decision.verdict is Verdict.SILENT
            if decision.verdict is Verdict.ROUTE or silent_ok:
                pending = {**body_scan.vars, **post_scan.vars}   # post wins on conflict

            signal = move.signal
            if signal == SIG_FAILED:
                message = (f"body died: rc={result.rc}, {total} attempt(s)"
                           f"{' (fallback tried)' if fallback_used else ''}; "
                           f"stderr: {_tail(result.stderr)}")
            elif signal == SIG_DEFAULT:
                message = f"no known signal emitted; stdout: {_tail(result.stdout)}"
            elif signal is None:
                message = ""                        # pool silent ok
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
                model=agent_spec.model if agent_spec else None,
                failure_class=last_failure_class if signal == SIG_FAILED else None,
                recorded_signal=post_scan.first_known or body_scan.first_known,
                recorded_body=post_scan.first_body or body_scan.first_body,
                pending_vars=pending, updates=updates,
            )

    def _prepare_body(self, action: Action, node: Node, step_dir: Path,
                      render_fn, phase: str, inherited_prompt: str | None = None):
        """Returns (command_for_procrun, rendered_prompt_text_or_None, rendered_AgentSpec_or_None).

        Every scalar agent field is a template (contract: an ensemble is just a pool
        with per-input harness/model). Optional fields rendering empty count as absent."""
        if action.kind == "shell":
            return render_fn(action.shell, "shell"), None, None

        spec = action.agent
        harness = render_fn(spec.harness, "agent.harness").strip()
        model = render_fn(spec.model, "agent.model", required=False) if spec.model else None
        effort = render_fn(spec.effort, "agent.effort", required=False) if spec.effort else None
        # an arg rendering empty is absent (never the literal template text back)
        args = [r for a in spec.args
                if (r := render_fn(a, "agent.args", required=False)).strip()]
        from .model import AgentSpec
        rendered_spec = AgentSpec(harness=harness, model=model or None,
                                  effort=effort or None, args=args)

        adapter = resolve_harness(rendered_spec)
        if action.prompt is not None:
            prompt_text = render_fn(action.prompt, "prompt")
        elif inherited_prompt is not None:
            prompt_text = inherited_prompt      # fallback reuses the primary's rendered prompt
        else:
            raise EngineCrash(E_RENDER, "agent action has no prompt", node=node.name)
        prompt_file = step_dir / ("prompt.md" if phase == "primary" else "prompt-fallback.md")
        prompt_file.write_text(prompt_text, encoding="utf-8")
        return adapter.build_argv(rendered_spec, prompt_file), prompt_text, rendered_spec

    # ── decision node: the seam + decision-node policy (fold law application) ──
    def _run_decision(self, node: Node, step_dir: Path, step_no: int):
        known = self._known(node)
        render_fn = lambda text, what, required=True: self._render_or_crash(
            text, node, what, required)
        outcome = self._run_attempts(
            node, step_dir, render_fn,
            apply_pre_vars=self._apply_vars,    # decision = max_parallel 1: vars apply live
            attempt_ns=f"{step_no:03d}", known=known,
        )
        for u in outcome.updates:
            log(f"update: {u}")
        self._apply_vars(outcome.pending_vars)  # body/post vars: routed outcome only
        return outcome.signal, outcome.message, {
            "attempts": outcome.attempts, "rc": outcome.rc, "timed_out": outcome.timed_out,
            "fallback": outcome.fallback_used, "harness": outcome.harness,
            "model": outcome.model,
        }

    # ── pool machinery ────────────────────────────────────────────────────────

    def _materialize_inputs(self, node: Node, step_dir: Path) -> list:
        """Snapshot inputs into steps/NNN-<node>/inputs.json (resume foundation)."""
        spec = node.pool.inputs
        if spec.data is not None:
            inputs = list(spec.data)
        else:
            cmd = self._render_or_crash(spec.shell, node, "inputs.shell")
            res = proc_run(cmd, self.workdir, self._clamp(spec.shell_timeout),
                           extra_env=self._base_env(),
                           log_path=step_dir / "inputs-source.txt")
            if res.timed_out:
                rem = self._remaining()
                if rem is not None and rem <= 0:      # the run budget killed it, not its own limit
                    raise EngineCrash(E_DEADLINE,
                                      f"pipeline timeout ({self.p.timeout}s) exhausted "
                                      f"while sourcing inputs", node=node.name)
                raise EngineCrash(E_INPUTS, f"inputs source timed out ({spec.shell_timeout}s)",
                                  node=node.name)
            if res.rc != 0:
                raise EngineCrash(
                    E_INPUTS,
                    f"inputs source exited rc={res.rc} (a broken producer is not an "
                    f"empty queue); stderr: {_tail(res.stderr)}",
                    node=node.name)
            inputs = _sniff_inputs(res.stdout, node.name)
        if len(inputs) > INPUTS_HARD_CAP:
            raise EngineCrash(E_INPUTS_LIMIT,
                              f"{len(inputs)} inputs (cap {INPUTS_HARD_CAP}) — "
                              f"this is almost certainly not what you wanted",
                              node=node.name)
        if any(isinstance(v, list) for v in inputs):
            raise EngineCrash(E_INPUTS, "array inputs are forbidden (wrap in an object)",
                              node=node.name)
        kinds = {isinstance(v, dict) for v in inputs}
        if len(kinds) > 1:
            raise EngineCrash(E_INPUTS, "mixed scalar/object inputs from source",
                              node=node.name)
        (step_dir / "inputs.json").write_text(
            json.dumps(inputs, ensure_ascii=False, indent=1), encoding="utf-8")
        return inputs

    def _run_one_input(self, node: Node, step_dir: Path, step_no: int,
                       idx: int, value, count: int,
                       pool_vars: dict[str, str], sequential: bool) -> dict:
        """Execute one input through the seam; returns a manifest row.
        Never raises for input-level failures; deadline crashes propagate."""
        t0 = time.monotonic()
        input_dir = step_dir / f"input-{idx:04d}"     # per-input namespace: no file races
        input_dir.mkdir(exist_ok=True)
        key = f"{idx}:{_input_hash(value)}"
        local_ctx: dict[str, str] = {}

        input_env = {
            "MEDULLA_INPUT": value if isinstance(value, str) else json.dumps(value, ensure_ascii=False),
            "MEDULLA_INPUT_INDEX": str(idx),
            "MEDULLA_INPUT_COUNT": str(count),
            "MEDULLA_INPUT_KEY": key,
        }
        if isinstance(value, dict):
            for k, v in value.items():
                if isinstance(v, (str, int, float, bool)) and VAR_NAME_RE.match(str(k)):
                    suffix = str(k).upper()
                    if suffix not in ("INDEX", "COUNT", "KEY"):
                        input_env[f"MEDULLA_INPUT_{suffix}"] = str(v)

        def render_fn(text: str, what: str, required: bool = True) -> str:
            # pool-input render semantics: breakage fails THIS input, never the run
            merged = {**pool_vars, **local_ctx} if not sequential else self.vars
            rendered = render(text, self.p.dir, merged,
                              input_value=value, has_input=True,
                              input_index=idx, input_count=count, last=self.last)
            if required and not rendered.strip():
                raise RenderError(f"{what} rendered empty")
            return rendered

        def env_fn() -> dict[str, str]:
            base = self._base_env(None if sequential else {**pool_vars, **local_ctx})
            return {**base, **input_env}

        def apply_pre_vars(pending: dict[str, str]) -> None:
            if sequential:
                self._apply_vars(pending)             # fold: ordered, not transactional
            else:
                # same blacklist as the sequential path — a parallel pre emitting
                # <signal:var key=PATH> must not poison this worker's subprocess env
                for k, v in pending.items():
                    if self._valid_var_key(k):
                        local_ctx[k] = v
                    else:
                        log(f"warn: var '{k}' rejected (reserved/invalid name)")

        row = {"index": idx, "key": key, "input": value}
        try:
            outcome = self._run_attempts(
                node, input_dir, render_fn, apply_pre_vars,
                attempt_ns=f"{step_no:03d}.i{idx}", known=None,
                env_fn=env_fn, pool_mode=True,
            )
        except RenderError as exc:
            row.update(ok=False, reason="render", signal=None, message=str(exc),
                       rc=None, timed_out=False, attempts=0, fallback=False,
                       harness=None, model=None, vars={}, updates=[],
                       duration_s=round(time.monotonic() - t0, 2), log=None)
            return row

        ok = outcome.signal not in (SIG_FAILED, SIG_DEFAULT)
        if outcome.guarded:
            reason = "guard"
        elif not ok:
            reason = outcome.failure_class or "rc"
        else:
            reason = "ok"
        # pool signals are data: surface the recorded bare signal in the row
        row_signal = outcome.signal if outcome.guarded else outcome.recorded_signal
        row_message = outcome.message if (outcome.guarded or not ok) else outcome.recorded_body
        pool_pre_vars = dict(local_ctx)               # >1: pre vars are row data too
        if sequential and ok:
            self._apply_vars(outcome.pending_vars)    # fold: next input sees them
        row.update(
            ok=ok, reason=reason, signal=row_signal, message=row_message,
            rc=outcome.rc, timed_out=outcome.timed_out, attempts=outcome.attempts,
            fallback=outcome.fallback_used, harness=outcome.harness, model=outcome.model,
            vars={**pool_pre_vars, **outcome.pending_vars} if ok else pool_pre_vars,
            updates=outcome.updates,
            duration_s=round(time.monotonic() - t0, 2),
            log=f"input-{idx:04d}/",
        )
        return row

    def _run_pool(self, node: Node, step_dir: Path, step_no: int):
        """Returns (signal, message, stats)."""
        manifest_path = step_dir / "manifest.jsonl"
        manifest_path.touch()                          # empty pools still register a manifest
        self.manifests[node.name] = manifest_path

        inputs = self._materialize_inputs(node, step_dir)
        total = len(inputs)
        pool = node.pool
        min_success = total if pool.min_success is None else pool.min_success
        if total == 0:
            return SIG_EMPTY, "source returned 0 inputs", {
                "inputs_total": 0, "inputs_ok": 0, "min_success": min_success}

        pool_vars = dict(self.vars)                    # snapshot: workers never read live vars
        workers = min(total, pool.max_parallel or total)
        sequential = workers == 1
        rows: list[dict] = []
        deadline_hit = False

        def guarded_run(idx: int, value):
            if self._remaining() is not None and self._remaining() <= 0:
                return None                            # never started: no row, resume re-runs it
            try:
                row = self._run_one_input(node, step_dir, step_no, idx, value,
                                          total, pool_vars, sequential)
            except EngineCrash as crash:
                if crash.code == E_DEADLINE:
                    return None                        # died of budget, not of its own timeout
                raise
            if row.get("timed_out") and not row["ok"] \
                    and self._remaining() is not None and self._remaining() <= 0:
                # killed by the shrinking run budget, not its own timeout: recording
                # this as reason:timeout would stop resume from ever re-running it
                return None
            return row

        if sequential:
            for i, value in enumerate(inputs, start=1):
                row = guarded_run(i, value)
                if row is None:
                    deadline_hit = True
                    break
                self.store.manifest_append(manifest_path, row)
                rows.append(row)
        else:
            import concurrent.futures
            first_crash: EngineCrash | None = None
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool_exec:
                futures = {pool_exec.submit(guarded_run, i, v): i
                           for i, v in enumerate(inputs, start=1)}
                for fut in concurrent.futures.as_completed(futures):
                    try:
                        row = fut.result()
                    except EngineCrash as crash:
                        # collect the rest before crashing — concluded inputs must not
                        # lose their manifest rows to an unrelated worker's crash
                        if first_crash is None:
                            first_crash = crash
                        continue
                    if row is None:
                        deadline_hit = True
                        continue
                    self.store.manifest_append(manifest_path, row)
                    rows.append(row)
            if first_crash is not None:
                raise first_crash

        if deadline_hit or (self._remaining() is not None and self._remaining() <= 0):
            raise EngineCrash(E_DEADLINE,
                              f"pipeline timeout ({self.p.timeout}s) exhausted mid-pool "
                              f"({len(rows)}/{total} inputs concluded; manifest rows survive)",
                              node=node.name)

        ok_count = sum(1 for r in rows if r["ok"])
        stats = {"inputs_total": total, "inputs_ok": ok_count, "min_success": min_success}
        if ok_count >= min_success:
            return SIG_DONE, f"{ok_count}/{total} inputs ok", stats
        by_class: dict[str, int] = {}
        for r in rows:
            if not r["ok"]:
                by_class[r["reason"]] = by_class.get(r["reason"], 0) + 1
        breakdown = ", ".join(f"{k} x{v}" for k, v in sorted(by_class.items()))
        ms_text = "all" if pool.min_success is None else str(min_success)
        return SIG_FAILED, (f"{ok_count}/{total} inputs ok (min_success={ms_text}); "
                            f"failures: {breakdown}"), stats

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
                           "signal": signal_name, "next": target, "duration_s": duration}
            if journal_kind == "pool":
                journal_row.update({k: stats.get(k) for k in
                                    ("inputs_total", "inputs_ok", "min_success")})
            else:
                journal_row.update({
                    "attempts": stats.get("attempts"), "rc": stats.get("rc"),
                    "timed_out": stats.get("timed_out"), "fallback": stats.get("fallback"),
                    "harness": stats.get("harness"), "model": stats.get("model"),
                })
            self.store.journal_append(journal_row)
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
