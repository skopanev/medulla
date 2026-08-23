"""The codex adapter."""
from __future__ import annotations

import json
import shutil

from ..errors import E_HARNESS, EngineCrash
from ..harness_base import (
    INNER_SLACK_S,
    HarnessAdapter,
    Invoke,
    _read_only,
)

# ── codex ────────────────────────────────────────────────────────────────────

class CodexAdapter(HarnessAdapter):
    name = "codex"
    session_key = "thread_id"    # codex calls the conversation a thread
    binary = "codex"

    def __init__(self):
        # A broker wrapper may stand in for codex (the container's `codex` can be
        # a shim into it), so availability is judged by the name the workflow
        # actually invokes — nothing here knows or cares about wrapper names.
        if not shutil.which("codex"):
            raise EngineCrash(
                E_HARNESS,
                f"harness '{self.name}': 'codex' not on PATH")

    def build(self, spec, prompt_file, prompt_text, timeout_s, resume=None):
        bin_ = "codex"
        inner_ms = (int(timeout_s) + INNER_SLACK_S) * 1000
        # sandbox: codex has native modes, so this maps exactly. The historical
        # default stays `danger` — the container IS the sandbox for most workflows,
        # and tightening it silently would break every existing one.
        ro = _read_only(spec)
        # `codex exec resume <id>` is a SUBCOMMAND, not a flag: the id follows it and
        # the prompt still rides stdin. Building it as `exec --json ... resume <id>`
        # would be parsed as a prompt, so the resume word goes in right after exec.
        argv = [bin_, "exec", *(["resume", resume] if resume else []),
                "--json", "--skip-git-repo-check"]
        at = 4 if resume else 2
        argv.insert(at, "-s" if ro else "--dangerously-bypass-approvals-and-sandbox")
        if ro:
            argv.insert(at + 1, "read-only")
        if spec.model:
            argv += ["-c", f'model="{spec.model}"']
        if spec.effort:
            argv += ["-c", f"model_reasoning_effort={spec.effort}"]
        argv += ["-c", f"stream_idle_timeout_ms={inner_ms}"]
        argv += spec.args                        # last -c wins: authors can override
        # stdin carries the COMPLETE prompt (no "Execute." prefix needed — that was
        # v1's convention for @file references); no ARG_MAX, no @-expansion coupling
        return Invoke(argv=argv, stdin=prompt_text)

    def extract_error(self, stdout: str) -> str | None:
        # codex reports real failure causes as stdout JSON events that the signal
        # filter rightly drops; without this, a turn.failed run yields a useless
        # __failed__ message (pilot scar: "0 output, exit 1")
        for line in stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") in ("error", "turn.failed") or event.get("error"):
                detail = event.get("message") or event.get("error") or event
                if isinstance(detail, (dict, list)):
                    detail = json.dumps(detail, ensure_ascii=False)
                return f"codex {event.get('type', 'error')}: {detail}"
        return None

    def filter_stdout(self, stdout: str) -> str:
        parts: list[str] = []
        for line in stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") != "item.completed":
                continue
            item = event.get("item", {})
            if item.get("type") == "agent_message":
                parts.append(item.get("text", ""))
            # command_execution aggregated_output is TOOL OUTPUT — never scanned.
            # (v1's <500-char exception violated the contract; dropped.)
        return "\n".join(p for p in parts if p)

    def stream_line(self, line: str) -> str | None:
        line = line.strip()
        if not line.startswith("{"):
            return None
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return None
        item = event.get("item", {})
        if event.get("type") == "item.completed" and item.get("type") == "agent_message":
            return item.get("text") or None
        if event.get("type") == "item.started" and item.get("type") == "command_execution":
            return f"$ {item.get('command', '')}"     # progress, pilot-style
        return None


