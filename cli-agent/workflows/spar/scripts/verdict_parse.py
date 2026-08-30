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
    for line in text.splitlines():
        if line.startswith("#"):
            head = line.strip("# \t").upper()
            cur = None
            # A REPEAT of the same heading continues the same section rather than
            # opening a rival one: the fenced example makes a panelist write
            # `## VERDICT` twice, and the second one carries the actual verdict.
            if head == want:
                exact = cur = exact if exact is not None else []
            elif head.startswith(want):
                prefix = cur = prefix if prefix is not None else []
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
    return bool(re.match(r"\(?[RG]\)?\s+(HIGH|MED|LOW)\b", line))


def read_panelist(path: Path) -> dict:
    text = path.read_text(encoding="utf-8", errors="replace")
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
        clause = plain.split("—")[1] if "—" in plain else ""
        if CITATION.match(clause):
            cites = [int(n) for n in re.findall(r"\d+", clause)]
        else:
            unreadable = True
    if verdict_line and not word:
        malformed.append(f"verdict not one of GO/NO-GO/INSUFFICIENT: {verdict_line[:40]!r}")
    return {
        "slug": path.stem,
        "malformed": malformed,
        "verdict": word or None,
        "line": verdict_line,
        "cites_local": cites,
        "citation_unreadable": unreadable,
        "findings": findings,
    }

