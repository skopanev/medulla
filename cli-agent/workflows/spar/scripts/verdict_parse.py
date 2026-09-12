"""Reading ONE panelist file: its findings, its verdict, what it malformed.

Split from collect_verdict.py under the project's 250-line rule ($MAX_LOC).
This half is the part that faces a model's formatting; the other half faces
the gate that reads the result.
"""
from __future__ import annotations

import re
from pathlib import Path

NOT_PANELISTS = {"question.md", "verdict.md", "synthesized.md", "all-findings.md"}
SEVERITY_ORDER = {"HIGH": 1, "MED": 2, "LOW": 3}
VERDICT_WORDS = ("NO-GO", "INSUFFICIENT", "GO")     # NO-GO first: it is a prefix trap

# digits and separators only: "NO-GO — this breaks 3 callers" must not yield F3
CITATION = re.compile(r"^\s*[Ff]?\d+(\s*(?:,|and|/|&)\s*[Ff]?\d+)*\s*$")


def _section(text: str, heading: str) -> list[str]:
    """Lines under `## HEADING`, to the next heading. Case-insensitive.

    A panelist wrote `## Findings` and its whole list was dropped without a word —
    exact matching turns a model's formatting into silent data loss.
    """
    want = heading.strip("# ").upper()
    exact: list[str] | None = None
    prefix: list[str] | None = None
    cur: list[str] | None = None
    level = 0
    for line in text.splitlines():
        if line.startswith("#"):
            depth = len(line) - len(line.lstrip("#"))
            head = line.strip("# \t").upper()
            # A REPEAT of the same heading continues the same section rather than
            # opening a rival one: the fenced example makes a panelist write
            # `## VERDICT` twice, and the second one carries the actual verdict.
            if head == want:
                exact = cur = exact if exact is not None else []
                level = depth
                continue
            if head.startswith(want):
                prefix = cur = prefix if prefix is not None else []
                level = depth
                continue
            # A DEEPER HEADING IS STILL INSIDE THE SECTION. Panelists group their
            # findings under `### Finding 1`, `### Finding 2` and put the actual
            # `- (R) HIGH — ...` lines beneath those. Ending the section at the first
            # heading of ANY depth cut it off immediately after `## FINDINGS`, so the
            # findings were dropped — and `malformed` stayed empty, because the
            # HEADING was present and that is all it checks. The panelist sees a
            # complete artifact with the delivery marker, and the verdict records
            # zero findings. Measured across stored runs: three artifacts, 15
            # findings lost, two of them in the last two days.
            if cur is not None and depth > level:
                continue                     # a subheading is structure, not content
            cur = None
            continue
        # A fence is not content. The prompt used to show the verdict INSIDE triple
        # backticks, so a panelist copied the fence too and the first non-empty line
        # under the heading was ``` — parsed as "no verdict", which dropped a whole
        # delivered artifact from the count.
        if line.lstrip().startswith("```"):
            continue
        if cur is not None:
            cur.append(line)
    # Exact wins over prefix: `## Verdict reasoning` sits ABOVE `## VERDICT` in real
    # artifacts, and a prefix match handed back its first prose line as the verdict.
    # Prefix is still accepted alone, so `## FINDINGS (12)` keeps working.
    return exact if exact is not None else (prefix or [])


def _severity(rest: str) -> str:
    """The slot after (R)/(G), not a substring of the line."""
    slot = re.sub(r"^\(?[RG]\)?\s*", "", rest).split()
    return slot[0] if slot and slot[0] in SEVERITY_ORDER else ""


def _looks_like_finding(line: str) -> bool:
    """A bulletless line is a finding only with its severity attached.

    `(R) HIGH — ...` is the format; `(R) Reproduced directly:` is prose that happens
    to carry a confidence mark, and admitting it would invent a finding nobody made.
    """
    # An optional "1." / "12)" prefix is allowed: the verdict format asks panelists
    # to cite findings by NUMBER, so they number them — and requiring a bullet threw
    # every numbered finding away without a word.
    return bool(re.match(r"(?:\d+[.)]\s*)?\(?[RG]\)?\s+(HIGH|MED|LOW)\b", line))


def read_panelist(path: Path) -> dict:
    text = path.read_text(encoding="utf-8", errors="replace")
    # The COVERAGE line is deliberately excluded from findings (no bullet, no
    # severity) and was therefore read by nobody at all: the prompt demanded it, the
    # parser dropped it, and the caller could not see the un-swept surface it names.
    # A claim about what was NOT looked at is the difference between "nothing else is
    # wrong" and "I did not look" — carry it.
    coverage = ""
    for line in _section(text, "## FINDINGS"):
        m = re.match(r"\s*COVERAGE\s*:\s*(.+)", line, re.I)
        if m:
            coverage = m.group(1).strip()
            break
    findings = []
    for line in _section(text, "## FINDINGS"):
        stripped = line.strip()
        # A finding is a finding with or without a bullet. Gemini wrote eight of them
        # starting straight at "(R) HIGH — ...", and requiring "-" dropped all eight
        # silently — which then made its own "NO-GO — 1" cite a finding that, as far
        # as the parser was concerned, did not exist, and turned a supported objection
        # into an unsupported one.
        if not stripped.startswith(("-", "*")) and not _looks_like_finding(stripped):
            continue
        rest = re.sub(r"^\s*[-*]\s*", "", line)
        m = re.match(r"\(([RG])\)", rest)
        findings.append({
            "panelist": path.stem,
            "confidence": m.group(1) if m else "",
            "severity": _severity(rest),
            "text": rest.strip(),
        })

    verdict_line = next((l.strip() for l in _section(text, "## VERDICT") if l.strip()), "")
    # `**NO-GO**.` is a verdict. Emphasis is the model decorating, not deciding.
    plain = re.sub(r"[*_`]", "", verdict_line)
    # What the parser could not make sense of, kept as a fact rather than a silence.
    malformed = []
    if not re.search(r"(?im)^#+\s*FINDINGS\b", text):
        malformed.append("no FINDINGS heading")
    if not re.search(r"(?im)^#+\s*VERDICT\b", text):
        malformed.append("no VERDICT heading")
    word = next((w for w in VERDICT_WORDS if plain.startswith(w)), "")
    cites, unreadable = [], False
    if word == "NO-GO":
        # ANY dash, not just the em-dash: models substitute "-" and "--" freely, and
        # a split on the wrong character produced clause="" — the NO-GO was printed
        # as unsupported and blocked nothing. Strip the verdict WORD first: "NO-GO"
        # contains a hyphen itself, so splitting the whole line cuts inside it.
        tail = plain[len(word):]
        parts = re.split(r"\s*[—–-]+\s*", tail)
        clause = parts[1] if len(parts) > 1 else ""
        if CITATION.match(clause):
            cites = [int(n) for n in re.findall(r"\d+", clause)]
        else:
            unreadable = True
    if verdict_line and not word:
        malformed.append(f"verdict not one of GO/NO-GO/INSUFFICIENT: {verdict_line[:40]!r}")
    return {
        "slug": path.stem,
        "coverage": coverage,
        "malformed": malformed,
        "verdict": word or None,
        "line": verdict_line,
        "cites_local": cites,
        "citation_unreadable": unreadable,
        "findings": findings,
    }

