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
