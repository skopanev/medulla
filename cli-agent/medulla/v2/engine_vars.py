"""Vars and the environment children see: what a node may know, and what it may name.

Split from engine.py under the project's 250-line rule ($MAX_LOC). Two boundaries meet
here — the one between engine state and a child process (an oversized var is refused
here, not truncated in the kernel), and the one between what a node emits and what the
run remembers.
"""
from __future__ import annotations

import json
import os
import sys

from .contract import VAR_NAME_RE
from .engine_scan import log
from .errors import E_RENDER, EngineCrash
from .harness import resolve as resolve_harness
from .model import CHANNEL_SIGNALS, ENGINE_FACTS, ENV_BLACKLIST_EXACT, ENV_BLACKLIST_PREFIX, Node
from .render import RenderError, render


class VarsMixin:
    def _base_env(self, vars_map: dict[str, str] | None = None) -> dict[str, str]:
        source = self.vars if vars_map is None else vars_map
        for key, value in source.items():
            if isinstance(value, str):
                size = len(value.encode("utf-8", "surrogatepass"))
                if size > self._MAX_ENV_VALUE:
                    raise EngineCrash(
                        E_RENDER,
                        f"var '{key}' is {size} bytes — too large for the environment "
                        f"(limit {self._MAX_ENV_VALUE}). Linux caps one env string at "
                        f"131072 bytes, so every body would die with 'Argument list too "
                        f"long'. Pass it to the agent through the prompt "
                        f"({{{{var:{key}}}}}) and keep it out of shell bodies.")
        env = {**self.dotenv, **source}
        env["MEDULLA_RUN_ID"] = self.store.run_id
        # The PIPELINE is the outermost run, and it is inherited rather than reset:
        # every nested `medulla` overwrites MEDULLA_RUN_ID with its own, so anything
        # anchored to it splits at the first nesting. A develop unit hands its agent
        # session to a landing run started from one of its nodes — same pipeline, new
        # run id — and the container that holds the conversation must be the same one.
        env["MEDULLA_PIPELINE_ID"] = (
            os.environ.get("MEDULLA_PIPELINE_ID") or self.store.run_id)
        env["MEDULLA_RUN_DIR"] = str(self.store.dir)
        # Where the workflow's own files live — prompts/, scripts/, anything it ships.
        # A node that wants to call its workflow's own script had no way to find it:
        # the path depends on which copy resolved (repo-local or machine-wide) and, in
        # a container, on where that copy was mounted. Every other answer to "where am
        # I" is here, so this one belongs here too.
        if self.p.dir:
            env["MEDULLA_WORKFLOW_DIR"] = str(self.p.dir)
        # Stops HERE. It is an internal compensator (scripts/docker.py sets it when the
        # definition is mounted read-only), and bodies inherit os.environ wholesale — so
        # a `medulla` invoked from inside a body would root ITS history in OUR run
        # directory, and that workflow's keep_runs would then evict our history.
        # Observed live: a panelist's shell inherited it and 96 unrelated tests failed.
        env["MEDULLA_RUNS_UNDER"] = ""
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
        """Decision-context render: any breakage is a workflow bug -> E_RENDER.
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

    def _make_echo(self, node: Node):
        """Operator streaming: shell lines as-is (signals hidden), agent lines
        through the adapter's per-line renderer. Display channel ONLY — the
        signal scanner still reads the captured stdout post-hoc.
        MEDULLA_STREAM=0 silences (tests, CI)."""
        if os.environ.get("MEDULLA_STREAM", "1") == "0":
            return None
        action = node.action
        if action.kind == "shell":
            def echo(tag, line):
                from .harness import _ANSI_RE
                clean = _ANSI_RE.sub("", line.rstrip())
                if clean and not clean.lstrip().startswith("<signal:"):
                    print(f"  {clean}", file=sys.stderr)
            return echo
        try:
            adapter = resolve_harness(action.agent) if action.agent and                 "{{" not in action.agent.harness else None
        except EngineCrash:
            adapter = None
        def echo(tag, line):
            if tag != "out" or adapter is None:
                return
            rendered = adapter.stream_line(line)
            if not rendered:
                return
            shown = "\n".join(f"  {l}" for l in rendered.splitlines()
                              if l.strip() and not l.lstrip().startswith("<signal:"))
            if shown:
                print(shown, file=sys.stderr)
        return echo

    # ── the attempts seam ────────────────────────────────────────────────────
    # Owns the FULL hook machinery (panel: pre and post live INSIDE the seam so
    # part-3 pool workers get identical semantics by calling this per input):
    #   pre -> [guard?] -> body attempts (primary -> fallback) with post per attempt.
    # The seam never mutates engine state: `apply_pre_vars` is the caller's policy
    # (decision: apply to self.vars so the body render sees them; pool at
    # max_parallel>1: record to manifest + local ctx), and body/post vars come
    # back as pending_vars for the caller to apply per the fold law.

    def vars_the_action_may_set(self, action, produced: dict[str, str],
                                node_name: str) -> dict[str, str]:
        """Filter <signal:var> from a BODY by who wrote it.

        A shell body is code the workflow author committed; an agent body is a model's
        stdout. Only the first may hand a value to a later step. Anything an agent
        emits is honoured only when the node declared it:

            agent: {harness: codex, sets: [SCOPE, FINDINGS]}

        Default is empty, so a workflow written before this field gains the protection
        instead of keeping the old behaviour. Routing signals are untouched — a step
        saying what happened is how the graph works. This is only about a value a
        LATER, DIFFERENT step will trust: a frozen digest, a push destination, a
        working-tree fingerprint. One line of agent stdout used to retire all of them.

        An ignored attempt is LOGGED, never silently dropped — otherwise the next
        person debugging spends an hour on a value that never arrived.
        """
        if not produced or action.kind != "agent":
            return produced
        allowed = set(action.agent.sets or ())
        kept = {k: val for k, val in produced.items() if k in allowed}
        refused = [k for k in produced if k not in allowed]
        if refused:
            how = (f"declare them in agent.sets: {sorted(set(refused) | allowed)}"
                   if allowed else "agent.sets is empty — declare the ones it may set")
            log(f"  [{node_name}] ignored var(s) from the agent: {', '.join(sorted(refused))}"
                f" — {how}")
        return kept
