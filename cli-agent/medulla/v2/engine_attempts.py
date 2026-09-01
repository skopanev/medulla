"""One node, one body, N tries: the retry loop, its fallback phase, and the hooks around it.

Split from engine.py under the project's 250-line rule ($MAX_LOC). This is where an
attempt becomes an outcome: pre guard, fallback, and the post hook's last word.
"""
from __future__ import annotations

from pathlib import Path

from .classify import Move, Verdict, classify_attempt, next_move
from .engine_body import BodyMixin
from .engine_message import conclusion_message
from .engine_scan import (
    AttemptsOutcome,
    ScanResult,
    _retry_delay,
    _tail,
    _timeout_env,
    log,
    scan_stdout,
)
from .errors import E_HARNESS, EngineCrash
from .harness import resolve as resolve_harness
from .model import HOOK_TIMEOUT_S, SIG_FAILED, Node
from .procrun import run as proc_run
from .secret_policy import env_keys_to_remove


class AttemptsMixin(BodyMixin):
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
        echo=None,
    ) -> AttemptsOutcome:
        # env_fn: callable -> dict, the base env for hooks and bodies. Decision nodes
        # default to self._base_env (pre vars land in self.vars and are picked up on
        # the next call); part-3 pool workers pass their own (base + input ctx + local
        # pre-vars overlay) so parallel inputs never touch shared engine state.
        if env_fn is None:
            env_fn = self._base_env
        action = node.action

        pre_updates: list[str] = []
        pre_events: list[dict] = []
        guard, pre_updates, pre_events = self._run_pre_hook(
            node, step_dir, render_fn, apply_pre_vars, env_fn, known)
        if guard is not None:
            return guard

        post_rendered = render_fn(node.post, "post") if node.post else None

        current = action
        phase = "primary"
        fallback = self.p.action_fallback(action) if action.kind == "agent" else None
        fallback_used = False
        # contract: "primary gets N, then fallback gets N" — a fallback without its
        # own max_attempts inherits the primary's effective budget
        phase_budget = self.p.action_max_attempts(action)

        try:
            invoke, prompt_text, agent_spec = self._prepare_body(
                current, node, step_dir, render_fn, phase)
        except EngineCrash as exc:
            # Harness preflight failed (missing binary, agy trust preflight, unreadable
            # auth.json). In a POOL this input sits the round out and the other harnesses
            # still deliver — crashing here would abort every panelist for one broken
            # model, which min_success exists precisely to tolerate. A single-agent node
            # keeps the fatal (fail loud).
            if pool_mode and exc.code == E_HARNESS:
                return AttemptsOutcome(
                    signal=SIG_FAILED, message=exc.message, attempts=0,
                    failure_class="harness",
                )
            raise
        harness_name = agent_spec.harness if agent_spec else None

        attempt = 0
        total = 0
        limit_reason: str | None = None
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
                   "MEDULLA_HARNESS": tag,
                   **invoke.env}
            env_remove = list(invoke.env_remove)
            if current.kind == "agent" and agent_spec is not None:
                env_remove.extend(env_keys_to_remove(agent_spec.harness))

            result = proc_run(invoke.argv, self.workdir, eff, extra_env=env,
                              watch_output=(current.kind == "agent"),
                              idle_timeout_s=(agent_spec.idle_timeout
                                              if agent_spec else None),
                              log_path=step_dir / f"attempt-{total}-{tag}.txt",
                              stdin_data=invoke.stdin,
                              env_remove=sorted(set(env_remove)),
                              merge_stderr=invoke.merge_stderr, echo=echo,
                              hard_deadline=self.deadline)

            raw_text = result.stdout
            if current.kind == "agent":
                adapter = resolve_harness(agent_spec)
                # A plan/quota limit will not clear inside this run: stop retrying THIS
                # agent (a second attempt only burns time and looks like a mystery
                # "body died: rc=1"), but let a declared fallback take its turn — that
                # is exactly the case fallback exists for. Not fatal_error: crashing the
                # whole run would take the healthy panelists with it.
                # Checked BEFORE retry_pointless: a launcher that never ran cannot
                # have hit a quota, and saying "plan limit" about a missing module
                # sends the reader to the provider's dashboard for an hour.
                pointless = (adapter.broken_launch(result.stdout, result.stderr)
                             or adapter.retry_pointless(result.stdout))
                if pointless:
                    log(f"attempt {attempt_id}: {pointless}")
                    phase_budget = attempt          # no further attempts in this phase
                    limit_reason = pointless
                fatal = adapter.fatal_error(result.stdout)
                if fatal:
                    # deterministic environment failure (not logged in / bad key):
                    # retry and fallback are pointless — the razor call, same as
                    # agy's trust preflight. Found live: an unauthenticated claude
                    # burned attempts x inputs across a whole pool. In a POOL fail
                    # only THIS input (attempts spent, no retry) so the other
                    # harnesses still deliver; a single-agent node stays fatal.
                    if pool_mode:
                        return AttemptsOutcome(
                            signal=SIG_FAILED, message=fatal, attempts=total,
                            attempts_primary=n_primary, attempts_fallback=n_fallback,
                            rc=result.rc, timed_out=result.timed_out,
                            fallback_used=fallback_used, concluding_phase=phase,
                            harness=harness_name,
                            model=agent_spec.model if agent_spec else None,
                            failure_class="harness",
                        )
                    raise EngineCrash(E_HARNESS, fatal, node=node.name)
                self.capture_session(adapter, agent_spec, raw_text)
                raw_text = adapter.filter_stdout(raw_text)
            # A shell body's signals count only where the author wrote them. An agent
            # body keeps the lenient parse it needs (see
            # extract_signals) and is fenced by agent.sets instead.
            body_scan = scan_stdout(raw_text, known, strict=(current.kind == "shell"))

            post_rc = post_signal = None
            post_stderr = ""
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
                post_scan = scan_stdout(post_res.stdout, known, strict=True)  # hooks are shell
                post_rc, post_signal = post_res.rc, post_scan.first_known
                post_stderr = post_res.stderr
            delivery_confirmed = pool_mode and node.post_confirms_delivery and post_rc == 0
            # Pool signals are DATA, never classification; ignore_exit_code never applies.
            decision = classify_attempt(
                kind=current.kind, rc=result.rc, timed_out=result.timed_out,
                body_signal=None if pool_mode else body_scan.first_known,
                post_rc=post_rc,
                post_signal=None if pool_mode else post_signal,
                ignore_exit_code=(False if pool_mode
                                  else self.p.action_ignore_exit_code(current)), pool_mode=pool_mode,
                delivery_confirmed=delivery_confirmed,
            )
            move = next_move(
                decision, kind=current.kind, phase=phase, attempt=attempt,
                max_attempts=phase_budget,
                has_fallback=fallback is not None,
                pool_mode=pool_mode,
            )
            if decision.failure_class is not None:
                last_failure_class = ("watchdog" if result.killed_because
                                      else decision.failure_class)

            if move.move is Move.RETRY_SAME:
                log(f"attempt {attempt_id} failed (rc={result.rc}), retrying")
                _retry_delay()      # a zero-delay retry on a 429 is a provider-ban request
                continue
            if move.move is Move.SWITCH_FALLBACK:
                log(f"attempt {attempt_id} failed (rc={result.rc}), switching to fallback")
                _retry_delay()
                current = fallback
                phase = "fallback"
                attempt = 0
                fallback_used = True
                if fallback.max_attempts is not None:
                    phase_budget = fallback.max_attempts
                invoke, prompt_text, agent_spec = self._prepare_body(
                    current, node, step_dir, render_fn, phase, inherited_prompt=prompt_text)
                harness_name = agent_spec.harness if agent_spec else None
                continue

            # DONE — state signals are collected from a successful outcome only:
            # a ROUTED signal, or a pool's silent-ok (its vars ARE successful vars —
            # dropping them would make row.vars lie). A __default__ conclusion is a
            # communication failure: its vars must not leak (fold law).
            pending: dict[str, str] = {}
            updates = pre_updates + body_scan.updates + post_scan.updates
            events = pre_events + body_scan.events + post_scan.events
            silent_ok = pool_mode and decision.verdict is Verdict.SILENT
            if decision.verdict is Verdict.ROUTE or silent_ok:
                # The body's vars pass the who-wrote-this gate first; the post hook
                # is shell the author committed, so its vars are honoured as before.
                body_vars = self.vars_the_action_may_set(
                    current, body_scan.vars, node.name)
                pending = {**body_vars, **post_scan.vars}   # post wins on conflict

            signal = move.signal
            message = conclusion_message(
                signal, current, result, total, limit_reason, fallback_used,
                post_signal, post_scan, body_scan, known,
                agent_spec=agent_spec, post_rc=post_rc, post_stderr=post_stderr)
            if result.timed_out and post_rc:
                message += f"; post hook vetoed: {_tail(post_stderr)}"
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
                pending_vars=pending, updates=updates, signals=events,
            )
