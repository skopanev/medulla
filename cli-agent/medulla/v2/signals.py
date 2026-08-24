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


def extract_signals(text: str, strict: bool = False) -> list[tuple[str, dict[str, str], str]]:
    """strict=True: a signal counts only where it was WRITTEN — at the start of its
    own line. Off, the text is normalised first so a tag buried mid-line still
    matches, which is what agents need and what makes VALUES dangerous.

    A shell node that prints `<signal:done>pushed $sha</signal:done>` hands the engine
    whatever $sha contains. With normalisation on, a $sha carrying
    `</signal:done><signal:var key=ANCHOR>...` closes the tag early and opens a new
    one — and the value it interpolated becomes a variable a later step trusts.
    Reproduced end to end: a frozen ANCHOR came back REWRITTEN, exit 0.

    So the leniency is kept exactly where it was earned — agent output, where codex
    mirrors the prompt's "Label: <signal:...>" formatting and claude prints bare
    lines — and refused where the author's own code is the writer. An agent that
    abuses it is already fenced by agent.sets; a shell node is not, and should not
    need to be: it IS the author.
    """
    if not strict:
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


# The protocol text appended to every agent prompt: the grammar an agent must
# use to answer. It lives beside the parser that reads it back — a change to one
# without the other is how a run goes silent.
SIGNAL_PROTOCOL = """

## Signal protocol (engine-provided)

To emit a signal, print this template on its own line in your final message,
substituting {name} with the signal's name and the body with a short message
(no backticks, no quotes, keep the angle brackets exactly as shown):

<signal:{name}>short message</signal:{name}>

For example, a signal named finished would be printed as one line starting
with "<signal:" then "finished>", the message, and the matching closing tag.
Emit a signal only when the task tells you to. Print it as plain text in your
answer — never via a shell command or a file.
"""
