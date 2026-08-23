"""The agy adapter."""
from __future__ import annotations

import json
import os
from pathlib import Path

from ..errors import E_HARNESS, EngineCrash
from ..harness_base import (
    INNER_SLACK_S,
    HarnessAdapter,
    Invoke,
    _read_only,
    plain_text_signal_filter,
)

_AGY_SETTINGS = "~/.gemini/antigravity-cli/settings.json"

# convenience slugs -> exact `agy models` names (effort lives in the suffix)
AGY_MODEL_ALIASES = {
    "gemini-3.5-flash": "Gemini 3.5 Flash (Medium)",
    "gemini-3.5-flash-low": "Gemini 3.5 Flash (Low)",
    "gemini-3.5-flash-high": "Gemini 3.5 Flash (High)",
    "gemini-3.1-pro": "Gemini 3.1 Pro (High)",
    "gemini-3.1-pro-low": "Gemini 3.1 Pro (Low)",
    "gemini-3.1-pro-high": "Gemini 3.1 Pro (High)",
}


def _agy_trusted(workdir: Path) -> bool:
    try:
        with open(os.path.expanduser(_AGY_SETTINGS)) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return False
    cwd = os.path.realpath(workdir)
    for root in data.get("trustedWorkspaces") or []:
        if not isinstance(root, str):
            continue
        root = os.path.realpath(os.path.expanduser(root))
        if cwd == root or cwd.startswith(root + os.sep):
            return True
    return False


class AgyAdapter(HarnessAdapter):
    name = "agy"
    session_key = "conversation_id"
    binary = "agy"

    def prepare(self, spec, workdir):
        # Untrusted workspace makes --dangerously-skip-permissions HANG waiting for
        # interactive trust — deterministic, environment-level, unresolvable at
        # runtime: the E_HARNESS razor's spirit ("unresolvable"), not a flake.
        # In Docker the container is the sandbox and trust files are unreliable;
        # --print-timeout bounds any residual hang, so the guard is host-only.
        if os.environ.get("MEDULLA_DOCKER") == "1":
            return
        if not _agy_trusted(workdir):
            raise EngineCrash(
                E_HARNESS,
                f"agy: workspace '{workdir}' is not in trustedWorkspaces — "
                f"`agy --dangerously-skip-permissions` would hang forever. Trust it "
                f"once (open `agy` there interactively, or add the path to "
                f"\"trustedWorkspaces\" in {_AGY_SETTINGS}).")

    def build(self, spec, prompt_file, prompt_text, timeout_s, resume=None):
        # sandbox → agy `--mode plan`, exactly as claude maps it to `--permission-mode
        # plan`. This used to raise "not expressible": true when agy offered only the
        # boolean `--sandbox` (terminal restrictions, no write protection), stale since
        # `--mode (accept-edits, plan)` landed. Plan mode refuses writes at the CLI's
        # permission layer — the write RPC is rejected, not merely discouraged — so it is
        # a real lock, not a polite request (verified on agy 1.1.17: the file was never
        # created). Refusing a read-only step on a harness that supports it pushed panels
        # onto other models for no reason.
        ro = _read_only(spec)
        # stream-json, not the default text: `text` buffers the whole answer and hands
        # it over at the very end, so a live run is indistinguishable from a hung one —
        # measured on agy 1.1.18, every line landed at +15s together, while stream-json
        # emitted at +4s, +5s, +13s, +16s. It also gives agy the structured output this
        # file elsewhere says it lacks: the final text arrives as result.response
        # instead of being guessed out of console prose.
        argv = ["agy",
                *(["--mode", "plan"] if ro else ["--dangerously-skip-permissions"]),
                "--output-format", "stream-json",
                "--print-timeout", f"{int(timeout_s) + INNER_SLACK_S}s"]
        if spec.model:
            argv += ["--model", AGY_MODEL_ALIASES.get(spec.model, spec.model)]
        if resume:
            argv += ["--conversation", resume]   # before --print; see the note below
        argv += spec.args
        # --print MUST be last: it consumes the next token as the prompt value.
        # Any flag placed after it silently becomes the prompt (verified v1.0.4).
        argv += ["--print", prompt_text]
        return Invoke(argv=argv)

    def stream_line(self, line: str) -> str | None:
        """One NDJSON event -> what a watcher should see, or nothing."""
        try:
            ev = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            return None
        kind = ev.get("event")
        if kind == "step_update":
            step = ev.get("step_update") or {}
            if step.get("step_type") == "agent_response":
                return (step.get("text_delta") or "").rstrip("\n") or None
            # Tools, because answer text is scarce while real work happens: one panel
            # run emitted 54 agent_response events carrying only 2 non-empty deltas,
            # against 104 tool steps. Text alone would leave a working agent looking
            # idle for minutes — the very thing streaming is here to fix.
            if step.get("step_type") == "tool" and step.get("state") == "ACTIVE":
                info = step.get("tool_info") or {}
                params = info.get("parameters") or {}
                cmd = params.get("CommandLine") or params.get("command")
                name = step.get("tool_name") or info.get("name") or "tool"
                return f"$ {cmd}" if cmd else f"[{name}]"
            return None
        if kind == "result":
            status = (ev.get("result") or {}).get("status")
            return None if status == "SUCCESS" else f"agy: {status}"
        return None

    def filter_stdout(self, stdout: str) -> str:
        """Signals come from what agy SAID, never from what it printed.

        With --output-format stream-json the answer arrives as result.response, so the
        old line-start heuristic — the ceiling while agy had no structured output — is
        no longer the best available: tool output and console prose can no longer be
        mistaken for a signal, exactly as with claude-code and codex.
        """
        answer, saw_json = [], False
        for line in stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("event") in ("init", "step_update", "result"):
                saw_json = True     # OUR events only — see OpenCodeAdapter.filter_stdout
            if ev.get("event") == "result":
                res = ev.get("result") or {}
                # A FAILED turn can still carry a response, and a signal mined out of it
                # would route the graph as though the turn had succeeded.
                if res.get("status") not in (None, "SUCCESS"):
                    return ""
                text = res.get("response")
                if text:
                    return text
            elif ev.get("event") == "step_update":
                step = ev.get("step_update") or {}
                if step.get("step_type") == "agent_response" and step.get("text_delta"):
                    answer.append(step["text_delta"])
        if answer:
            return "".join(answer)        # no result event (killed mid-turn): use the deltas
        # Not stream-json at all — an older agy, or plain text on stderr. Fall back to
        # the heuristic rather than returning nothing.
        return "" if saw_json else plain_text_signal_filter(stdout)


