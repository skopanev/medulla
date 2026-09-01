"""Turning a node's action into something runnable: the command, or the agent invocation.

Split from engine_attempts.py under the project's 250-line rule ($MAX_LOC). Every
scalar agent field is a template — an ensemble is just a pool with a per-input harness —
so this is also the place where a field that renders empty becomes an absent field.
"""
from __future__ import annotations

import os
from pathlib import Path

from .engine_scan import AttemptsOutcome, _tail, _timeout_env, log, scan_stdout
from .errors import E_RENDER, EngineCrash
from .harness import resolve as resolve_harness
from .model import HOOK_TIMEOUT_S, SIG_FAILED, Action, Node
from .procrun import run as proc_run
from .signals import SIGNAL_PROTOCOL


class BodyMixin:
    def _prepare_body(self, action: Action, node: Node, step_dir: Path,
                      render_fn, phase: str, inherited_prompt: str | None = None):
        """Returns (Invoke, rendered_prompt_text_or_None, rendered_AgentSpec_or_None).

        Every scalar agent field is a template (contract: an ensemble is just a pool
        with per-input harness/model). Optional fields rendering empty count as absent."""
        from .harness import Invoke
        if action.kind == "shell":
            # BASH, not $SHELL. A workflow is code committed to a repo and must behave the
            # same everywhere it runs; $SHELL makes it behave like whatever the operator
            # happens to use. On a mac that is zsh, which does NOT word-split an unquoted
            # parameter — so `for x in $list` silently iterates ONCE over the whole blob.
            # Found live: a stage read a 40-line list, looped once, produced nothing, and
            # still signalled ready; in the container ($SHELL unset → bash) the same file
            # worked. Honour MEDULLA_SHELL for anyone who deliberately wants another one.
            shell = os.environ.get("MEDULLA_SHELL", "bash")
            rendered = render_fn(action.shell, "shell")
            return Invoke(argv=[shell, "-lc", rendered]), None, None

        spec = action.agent
        harness = render_fn(spec.harness, "agent.harness").strip()
        model = render_fn(spec.model, "agent.model", required=False) if spec.model else None
        effort = render_fn(spec.effort, "agent.effort", required=False) if spec.effort else None
        sandbox = render_fn(spec.sandbox, "agent.sandbox", required=False) if spec.sandbox else None
        # an arg rendering empty is absent (never the literal template text back)
        args = [r for a in spec.args
                if (r := render_fn(a, "agent.args", required=False)).strip()]
        session = (render_fn(spec.session, "agent.session", required=False).strip()
                   if spec.session else "")
        from .model import AgentSpec
        rendered_spec = AgentSpec(harness=harness, model=model or None,
                                  effort=effort or None, idle_timeout=spec.idle_timeout,
                                  sandbox=sandbox or None,
                                  args=args, sets=spec.sets, session=session or None)

        adapter = resolve_harness(rendered_spec)
        adapter.prepare(rendered_spec, self.workdir)   # idempotent preflight (agy trust, opencode.json)
        if action.prompt is not None:
            prompt_text = render_fn(action.prompt, "prompt")
        elif inherited_prompt is not None:
            prompt_text = inherited_prompt      # fallback reuses the primary's rendered prompt
        else:
            raise EngineCrash(E_RENDER, "agent action has no prompt", node=node.name)
        prompt_file = step_dir / ("prompt.md" if phase == "primary" else "prompt-fallback.md")
        # the protocol must ride in whatever text actually reaches the agent —
        # file (claude), stdin (codex) AND argv (opencode/agy). Battle test t2
        # found it riding in the file only: stdin/argv harnesses never saw it.
        # inherited prompt_text stays clean so a fallback doesn't double-stamp.
        full_prompt = prompt_text + SIGNAL_PROTOCOL
        prompt_file.write_text(full_prompt, encoding="utf-8")
        timeout_s = self._clamp(self.p.action_timeout(action))
        # Continue the named conversation if this run already opened one. The FIRST
        # node to name it starts it; the id is recorded when that node's body returns.
        # A named session whose id is not on file yet is not an error — it is the
        # first turn.
        entry = self.store.session_entry(session) if session else None
        if entry and entry.get("harness") != harness:
            # A claude session id handed to codex is not a resume, it is a lookup that
            # fails inside the CLI — which surfaces as an agent that "just did not
            # answer". Say it here, where the name and both harnesses are still known.
            raise EngineCrash(
                E_RENDER,
                f"session '{session}' was opened by harness "
                f"'{entry.get('harness')}' and this node runs '{harness}' — a "
                f"conversation cannot move between CLIs. Use a different session name "
                f"for the {harness} node.",
                node=node.name)
        resume = entry.get("id") if entry else None
        if resume:
            log(f"  [{node.name}] continuing session '{session}' ({resume})")
        invoke = adapter.build(rendered_spec, prompt_file, full_prompt, timeout_s,
                               resume=resume)
        return invoke, prompt_text, rendered_spec

    # ── decision node: the seam + decision-node policy (fold law application) ──

    def _run_pre_hook(self, node, step_dir, render_fn, apply_pre_vars, env_fn, known):
        """Run the pre hook, if any. Returns (guard_outcome_or_None, updates, events).

        The guard is the point of the hook: one that emits a KNOWN signal routes the node
        and the body never runs — "skip unless the branch moved" belongs in a hook, not in
        a node of its own. A non-zero rc with no signal fails the step before the body,
        which is why it is reported with failure_class "pre" and not as a body failure.
        """
        updates: list[str] = []
        events: list[dict] = []
        if node.pre is None:
            return None, updates, events

        # Render the harness before it becomes a tag: in a pool `harness:` is itself a
        # template ("{{input.harness}}"), and an unrendered one leaked into the pre hook
        # as MEDULLA_HARNESS and into the attempt log name. A hook keying off it (skip
        # unless opencode) silently matched nothing. Render failures keep the raw value —
        # this is a label, never a reason to crash a step.
        action = node.action
        tag = "shell" if action.kind == "shell" else action.agent.harness
        if action.kind != "shell":
            try:
                tag = render_fn(action.agent.harness, "harness") or tag
            except EngineCrash:
                pass

        timeout = self._clamp(HOOK_TIMEOUT_S)
        env = {**env_fn(), "MEDULLA_TIMEOUT_S": _timeout_env(timeout), "MEDULLA_HARNESS": tag}
        res = proc_run(render_fn(node.pre, "pre"), self.workdir, timeout,
                       extra_env=env, log_path=step_dir / "pre.txt",
                       hard_deadline=self.deadline)
        scan = scan_stdout(res.stdout, known, strict=True)   # the pre hook is shell
        updates, events = scan.updates, scan.events

        # a known signal wins over rc — same grammar as everywhere else
        if scan.first_known is not None:
            apply_pre_vars(scan.vars)      # env prep applies before the guard routes
            return AttemptsOutcome(        # guard: body and post are skipped
                signal=scan.first_known, message=scan.first_body,
                attempts=0, rc=res.rc, guarded=True, updates=updates, signals=events,
            ), updates, events
        if res.rc != 0:
            return AttemptsOutcome(
                signal=SIG_FAILED,
                message=f"pre hook failed: rc={res.rc}; stderr: {_tail(res.stderr)}",
                attempts=0, rc=res.rc, timed_out=res.timed_out,
                failure_class="pre", updates=updates, signals=events,
            ), updates, events

        apply_pre_vars(scan.vars)          # env prep BEFORE the body renders
        return None, updates, events

    def capture_session(self, adapter, agent_spec, raw_stdout: str) -> None:
        """Record the conversation id this attempt just created, if the node named one.

        Called BEFORE filter_stdout: the id lives in the CLI's own event stream, which
        the filter exists to throw away. Called on every attempt rather than only a
        successful one — a retry that dies still opened a conversation, and the store
        keeps the first id it is given.
        """
        if agent_spec is None or not agent_spec.session:
            return
        sid = adapter.session_id(raw_stdout)
        if sid:
            self.store.record_session(agent_spec.session, sid, agent_spec.harness)
