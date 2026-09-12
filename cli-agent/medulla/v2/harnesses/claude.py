"""The claude adapter."""
from __future__ import annotations

import json

from ..harness_base import (
    INNER_SLACK_S,
    HarnessAdapter,
    Invoke,
    _read_only,
)

# ── claude-code ──────────────────────────────────────────────────────────────

class ClaudeAdapter(HarnessAdapter):
    name = "claude-code"
    binary = "claude"
    session_key = "session_id"

    def build(self, spec, prompt_file, prompt_text, timeout_s, resume=None):
        # sandbox → claude permission modes. `plan` is the only mode that refuses
        # edits outright, so it is what read-only means here.
        ro = _read_only(spec)
        perm = ["--permission-mode", "plan"] if ro else ["--dangerously-skip-permissions"]
        argv = ["claude", *perm,
                "--output-format", "stream-json", "--verbose"]
        if spec.model:
            argv += ["--model", spec.model]
        if spec.effort:
            argv += ["--effort", spec.effort]   # low|medium|high|xhigh|max (claude --help)
        if resume:
            argv += ["--resume", resume]     # same conversation, second turn
        argv += spec.args
        # THE TASK IS THE USER MESSAGE, as it already is for codex and opencode. It
        # used to go in as an appended SYSTEM prompt while the user message was the
        # literal "Execute." — a v1 convention that outlived its reason here. The
        # agent still saw the task, so nothing looked broken; what could not see it
        # was anything listening to the user message. Measured downstream: a hook on
        # UserPromptSubmit retrieved against the string "Execute." and returned
        # unrelated memories, while the same store answered precisely when asked with
        # the real task. `--resume` made it permanent — every turn of a continued
        # conversation repeated the same placeholder.
        argv += ["-p"]
        inner_ms = (int(timeout_s) + INNER_SLACK_S) * 1000
        return Invoke(argv=argv, stdin=prompt_text,
                      env={"API_TIMEOUT_MS": str(inner_ms)},
                      env_remove=["ANTHROPIC_API_KEY"])   # the OAuth account must win

    def filter_stdout(self, stdout: str) -> str:
        parts: list[str] = []
        for line in stdout.splitlines():
            line = line.strip()
            if not line or not line.startswith("{"):
                continue                    # non-JSON preamble is never assistant text
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = event.get("type", "")
            if etype == "assistant":
                for block in event.get("message", {}).get("content", []):
                    if isinstance(block, dict) and block.get("type") == "text":
                        parts.append(block.get("text", ""))
            elif etype == "content_block_delta":
                delta = event.get("delta", {})
                if delta.get("type") == "text_delta":
                    parts.append(delta.get("text", ""))
            elif etype == "result":
                raw = event.get("result", "")
                if isinstance(raw, str):
                    parts.append(raw)
                elif isinstance(raw, dict):         # dict-shaped final result (pilot scar):
                    parts.append(raw.get("output", ""))  # dropping it = permanent __default__
            # user messages (tool_result), tool_use blocks, system events: SKIP
        return "\n".join(p for p in parts if p)

    def retry_pointless(self, stdout: str) -> str | None:
        # "You've hit your weekly limit · resets Aug 21, 3pm (UTC)" arrives as a normal
        # error result with api_error_status 429, so the engine saw only "body died:
        # rc=1" and spent a second attempt on something that cannot change until the reset.
        for line in stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "rate_limit_event":
                info = event.get("rate_limit_info") or {}
                if info.get("status") == "rejected":
                    kind = str(info.get("rateLimitType") or "rate").replace("_", "-")
                    return f"claude-code: {kind} plan limit reached — retrying cannot help"
            if event.get("type") == "result" and event.get("api_error_status") == 429:
                return f"claude-code: {str(event.get('result', 'rate limited')).strip()}"
        return None

    def fatal_error(self, stdout: str) -> str | None:
        for line in stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "result" and event.get("is_error"):
                text = str(event.get("result", ""))
                if "Not logged in" in text or "Invalid API key" in text \
                        or "/login" in text:
                    return f"claude-code is not authenticated: {text!r} — " \
                           f"run `claude /login` (in docker: keychain-bound OAuth " \
                           f"does not reach the container; use CLAUDE_CODE_OAUTH_TOKEN)"
        return None

    def stream_line(self, line: str) -> str | None:
        line = line.strip()
        if not line.startswith("{"):
            return None
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return None
        if event.get("type") == "assistant":
            texts = [b.get("text", "") for b in event.get("message", {}).get("content", [])
                     if isinstance(b, dict) and b.get("type") == "text"]
            out = " ".join(x for x in texts if x).strip()
            return out or None
        return None


