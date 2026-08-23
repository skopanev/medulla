"""The opencode adapter."""
from __future__ import annotations

import json

from ..harness_base import (
    INNER_SLACK_S,
    HarnessAdapter,
    Invoke,
    _read_only,
    plain_text_signal_filter,
)

# ── opencode ─────────────────────────────────────────────────────────────────

class OpenCodeAdapter(HarnessAdapter):
    name = "opencode"
    binary = "opencode"

    def build(self, spec, prompt_file, prompt_text, timeout_s):
        # --format json for the same reason as agy: the default buffers the answer and
        # hands it over at the end, so a working panelist looks like a hung one
        # (measured on opencode 1.18.19: events at +2s/+3s versus one block at the
        # close). It also ends the guessing — the answer arrives as `text` parts, so
        # tool output can no longer be mistaken for a signal.
        argv = ["opencode", "run", "--format", "json", "--agent", "build"]
        if spec.model:
            argv += ["-m", spec.model]
        argv += spec.args
        # The prompt rides STDIN, not positional argv — as in CodexAdapter. Linux
        # caps a SINGLE argv string at MAX_ARG_STRLEN=131072 bytes, so a positional
        # prompt made every run with a >128KB prompt die with E2BIG. Model/effort
        # flags stay on argv: they are tiny.
        # Config rides in OPENCODE_CONFIG_CONTENT (ported from a parallel v1 fix,
        # main@217f751): the old on-disk opencode.json lingered in the workdir,
        # was stale-reused across runs, needed a TOCTOU lock under parallel
        # pools, and forced one shared config per workdir. The env layers on
        # top of any real project config and is naturally PER-INVOCATION —
        # heterogeneous per-input efforts now just work.
        # sandbox → opencode permissions. It has no read-only flag; the config is
        # the only lever, so deny the tools that mutate. bash is denied for
        # read-only because a shell is a write primitive.
        ro = _read_only(spec)
        # bash is denied too: a shell is a write primitive, so allowing it would make
        # read-only a label rather than a constraint.
        perm: object = ({"edit": "deny", "write": "deny", "patch": "deny", "bash": "deny"}
                        if ro else "allow")
        data: dict = {"$schema": "https://opencode.ai/config.json", "permission": perm}
        if spec.model and "/" in spec.model:
            provider, model_id = spec.model.split("/", 1)
            inner_ms = (int(timeout_s) + INNER_SLACK_S) * 1000
            pblock: dict = {"options": {"timeout": inner_ms}}
            if spec.effort:
                pblock["models"] = {model_id: {"options": {"reasoningEffort": spec.effort}}}
            data["provider"] = {provider: pblock}
        # opencode's run mode emits EVERYTHING (assistant text included) on
        # stderr with ANSI decoration; --format json is half-alive on 1.15.5
        # (single step_start event, rc 0 — probed live). Pilot's scar: merge
        # the streams, then filter hard.
        return Invoke(argv=argv, stdin=prompt_text, merge_stderr=True,
                      env={"OPENCODE_CONFIG_CONTENT": json.dumps(data)})

    def stream_line(self, line: str) -> str | None:
        """One JSON event -> what a watcher should see, or nothing."""
        try:
            ev = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            return None
        if ev.get("type") == "text":
            return ((ev.get("part") or {}).get("text") or "").rstrip("\n") or None
        return None

    def filter_stdout(self, stdout: str) -> str:
        """Signals come from what opencode SAID, never from what it printed.

        With --format json the answer arrives as `text` parts, so the line-start
        heuristic — the ceiling while opencode had no structured output — is no longer
        the best available. Anything that is not JSON at all still falls back to it,
        for an older opencode or for plain text on stderr.
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
            # OUR events only. Any line starting with `{` used to count, so an agent
            # that printed a JSON snippet in its answer disarmed the fallback and every
            # signal in that answer was dropped.
            if ev.get("type") in ("text", "step_start", "step_finish", "tool_use"):
                saw_json = True
            if ev.get("type") == "text":
                text = (ev.get("part") or {}).get("text")
                if text:
                    answer.append(text)
        if answer:
            return "".join(answer)
        return "" if saw_json else plain_text_signal_filter(stdout)


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


