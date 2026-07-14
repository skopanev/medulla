import json
import re

SIGNAL_RE = re.compile(
    r"(?ms)^[ \t]*<signal:([a-zA-Z0-9_-]+)([^>]*)>(.*?)</signal:\1>[ \t]*$"
)


def parse_var_attr(attr_text: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    m = re.search(r"key\s*=\s*\"?([^\">\s]+)", attr_text)
    if m:
        attrs["key"] = m.group(1)
    return attrs


def extract_signals(text: str) -> list[tuple[str, dict[str, str], str]]:
    # Strip backticks, then isolate each signal onto its own line so the
    # line-anchored SIGNAL_RE matches even when the model prefixes it, e.g.
    # "Signal: <signal:work>ready</signal:work>" or "1. Reason: <signal:var ...>".
    # codex mirrors the prompt's "Label:" formatting; claude emitted bare lines.
    text = re.sub(r"`(<signal:[^`]+)`", r"\1", text)
    text = re.sub(r"(?<=[^\n])(<signal:)", r"\n\1", text)
    text = re.sub(r"(</signal:[a-zA-Z0-9_-]+>)(?=[^\n])", r"\1\n", text)
    out: list[tuple[str, dict[str, str], str]] = []
    for m in SIGNAL_RE.finditer(text):
        name = m.group(1)
        attrs = parse_var_attr(m.group(2) or "")
        body = (m.group(3) or "").strip()
        out.append((name, attrs, body))
    return out


def extract_text_from_json(json_line: str) -> str:
    try:
        obj = json.loads(json_line)
    except (json.JSONDecodeError, ValueError):
        return ""
    if not isinstance(obj, dict):
        return ""
    msg_type = obj.get("type", "")
    if msg_type == "content_block_delta":
        delta = obj.get("delta", {})
        return delta.get("text", "")
    if msg_type == "assistant":
        content = obj.get("message", {}).get("content", [])
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(parts)
    # Codex JSON: item.completed wraps agent_message (text) and command_execution (tool output).
    if msg_type == "item.completed":
        item = obj.get("item", {})
        if item.get("type") == "agent_message":
            return item.get("text", "")
        if item.get("type") == "command_execution":
            # Extract from command output only if it's a short signal emission (printf/echo).
            # Skip large outputs (file reads, builds) that may contain echoed signal tags.
            output = item.get("aggregated_output", "")
            if len(output) < 500 and "<signal:" in output:
                return output
        return ""
    if msg_type in ("message_stop", "result"):
        content = obj.get("content", obj.get("result", ""))
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text", ""))
            return "\n".join(parts)
    return ""
