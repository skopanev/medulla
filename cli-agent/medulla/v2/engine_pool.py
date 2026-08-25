"""Pool nodes: many inputs through one body, and the manifest that records them.

Split from engine.py under the project's 250-line rule ($MAX_LOC), as a mixin rather
than a separate object: these methods are the Engine, they just answer a different
question — what happens when a node runs N times instead of once, and how a partial
delivery is still an answer.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .contract import VAR_NAME_RE
from .engine_inputs import InputsMixin
from .engine_scan import _input_hash, log
from .errors import E_DEADLINE, EngineCrash
from .model import (
    SIG_DEFAULT,
    SIG_DONE,
    SIG_EMPTY,
    SIG_FAILED,
    Node,
)
from .render import RenderError, render


class PoolMixin(InputsMixin):
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
                       harness=None, model=None, vars={}, updates=[], signals=[],
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
            updates=outcome.updates, signals=outcome.signals,
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
        deadline_hit = False

        # resume: seed the done-mask from existing rows — identity is (index, key),
        # never index alone (a changed input at the same index must re-run)
        old_rows = self.store.read_manifest(manifest_path)
        done = {(r.get("index"), r.get("key")) for r in old_rows if r.get("ok")}
        rows: list[dict] = list(old_rows)
        pending_inputs = [
            (i, v) for i, v in enumerate(inputs, start=1)
            if (i, f"{i}:{_input_hash(v)}") not in done
        ]
        if old_rows:
            log(f"pool resume: {len(done)} inputs done, {len(pending_inputs)} to run")

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
            for i, value in pending_inputs:
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
                           for i, v in pending_inputs}
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
            # Count what ALREADY succeeded before declaring the run lost. A pool waits
            # for every input by design — the point of five panelists is five answers —
            # but a deadline is not a reason to throw away an answer that is already on
            # disk. Reported live: min_success was met, two artifacts were written, and
            # the run still crashed E_DEADLINE with an empty journal, so nothing
            # downstream ever saw the work.
            done_keys = {(r.get("index"), r.get("key")) for r in rows if r.get("ok")}
            done = sum(1 for i, v in enumerate(inputs, start=1)
                       if (i, f"{i}:{_input_hash(v)}") in done_keys)
            if done >= min_success:
                log(f"  [{node.name}] deadline reached with {done}/{total} inputs ok "
                    f"(min_success {min_success}) — concluding on what delivered")
                return SIG_DONE, f"{done}/{total} inputs ok (deadline)", {
                    "inputs_total": total, "inputs_ok": done,
                    "min_success": min_success, "deadline": True,
                }
            raise EngineCrash(E_DEADLINE,
                              f"workflow timeout ({self.p.timeout}s) exhausted mid-pool "
                              f"({len(rows)}/{total} inputs concluded, {done} ok, "
                              f"min_success {min_success}; manifest rows survive)",
                              node=node.name)

        # join over old + new rows, keyed by input identity: an input is ok iff
        # ANY row matches its (index, key) with ok=true (stale/orphan rows inert)
        ok_keys = {(r.get("index"), r.get("key")) for r in rows if r.get("ok")}
        input_keys = [(i, f"{i}:{_input_hash(v)}") for i, v in enumerate(inputs, start=1)]
        ok_count = sum(1 for ik in input_keys if ik in ok_keys)
        stats = {"inputs_total": total, "inputs_ok": ok_count, "min_success": min_success}
        if ok_count >= min_success:
            return SIG_DONE, f"{ok_count}/{total} inputs ok", stats
        by_class: dict[str, int] = {}
        latest_by_key = {}
        for r in rows:
            latest_by_key[(r.get("index"), r.get("key"))] = r
        for ik in input_keys:
            if ik in ok_keys:
                continue
            row = latest_by_key.get(ik)
            reason = row["reason"] if row else "missing"
            by_class[reason] = by_class.get(reason, 0) + 1
        breakdown = ", ".join(f"{k} x{v}" for k, v in sorted(by_class.items()))
        ms_text = "all" if pool.min_success is None else str(min_success)
        return SIG_FAILED, (f"{ok_count}/{total} inputs ok (min_success={ms_text}); "
                            f"failures: {breakdown}"), stats

    # ── resume: rebuild engine state from the journal ──
