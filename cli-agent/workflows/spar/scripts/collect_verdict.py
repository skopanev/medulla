"""Panelist files -> verdict.md (for reading) and verdict.json (for gates).

    collect_verdict.py <run-dir> <round-dir> [--expected N] [--delivered N]
                       [--min-decided N]

Exit 0 with a verdict, 3 when fewer than --min-decided formed a GO/NO-GO.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

NOT_PANELISTS = {"question.md", "verdict.md", "synthesized.md", "all-findings.md"}
SEVERITY_ORDER = {"HIGH": 1, "MED": 2, "LOW": 3}
VERDICT_WORDS = ("NO-GO", "INSUFFICIENT", "GO")     # NO-GO first: it is a prefix trap

# digits and separators only: "NO-GO — this breaks 3 callers" must not yield F3
CITATION = re.compile(r"^\s*[Ff]?\d+(\s*(?:,|and|/|&)\s*[Ff]?\d+)*\s*$")


def _section(text: str, heading: str) -> list[str]:
    """Lines under `## HEADING`, to the next heading."""
    out, inside = [], False
    for line in text.splitlines():
        if line.startswith("## "):
            inside = line.strip() == heading
            continue
        if inside:
            out.append(line)
    return out


def _severity(rest: str) -> str:
    """The slot after (R)/(G), not a substring of the line."""
    slot = re.sub(r"^\(?[RG]\)?\s*", "", rest).split()
    return slot[0] if slot and slot[0] in SEVERITY_ORDER else ""


def read_panelist(path: Path) -> dict:
    text = path.read_text(encoding="utf-8", errors="replace")
    findings = []
    for line in _section(text, "## FINDINGS"):
        if not line.strip().startswith(("-", "*")):
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
    word = next((w for w in VERDICT_WORDS if verdict_line.startswith(w)), "")
    cites, unreadable = [], False
    if word == "NO-GO":
        clause = verdict_line.split("—")[1] if "—" in verdict_line else ""
        if CITATION.match(clause):
            cites = [int(n) for n in re.findall(r"\d+", clause)]
        else:
            unreadable = True
    return {
        "slug": path.stem,
        "verdict": word or None,
        "line": verdict_line,
        "cites_local": cites,
        "citation_unreadable": unreadable,
        "findings": findings,
    }


def build(round_dir: Path) -> dict:
    panelists = [read_panelist(p) for p in sorted(round_dir.glob("*.md"))
                 if p.name not in NOT_PANELISTS]

    # severity band, then each panelist's own order inside it
    numbered, order = [], []
    for rank in (1, 2, 3, 4):
        for p in panelists:
            for local, f in enumerate(p["findings"], start=1):
                if SEVERITY_ORDER.get(f["severity"], 4) == rank:
                    numbered.append({**f, "id": f"F{len(numbered) + 1}", "local": local})
                    order.append((p["slug"], local, len(numbered)))
    by_local = {(slug, local): n for slug, local, n in order}

    blocking, unsupported = [], []
    for p in panelists:
        ids = [f"F{by_local[(p['slug'], c)]}" for c in p["cites_local"]
               if (p["slug"], c) in by_local]
        p["cites"] = ids
        if p["verdict"] == "NO-GO":
            if ids:
                blocking.extend(ids)
            else:
                unsupported.append(p["slug"])      # fail closed: an objection is still one
                blocking.append(f"UNREAD-{p['slug']}")

    counts = {w: sum(1 for p in panelists if p["verdict"] == w)
              for w in ("GO", "NO-GO", "INSUFFICIENT")}
    counts["none"] = sum(1 for p in panelists if not p["verdict"])
    return {
        "panelists": panelists,
        "findings": numbered,
        "blocking": sorted({b for b in blocking if b.startswith("F")},
                           key=lambda b: int(b[1:])) + sorted(
                               {b for b in blocking if not b.startswith("F")}),
        "unsupported": unsupported,
        "counts": counts,
    }


HOW_TO_READ = """HOW TO READ THIS (rules, not suggestions):

- The verdicts are NOT a vote. GO 2 · NO-GO 2 is not a draw to break by counting — it
  is a list of findings to judge. Work the BLOCKING line.
- Carry EVERY finding forward by its id. A finding one panelist made is not weak — it
  is the one nobody else saw. Merge two only if they are literally the same claim,
  never because they feel similar.
- Do NOT re-summarise this file. It is already the compression; the prose it came from
  is in the per-panelist files beside it.
- (R) means the panelist verified it — opened the file, ran the command. (G) is a
  guess. Check a (G) before acting on it; do not discard it.
- FIX: is the panelist's proposed remedy, not an instruction. Judge it.
- INSUFFICIENT means that panelist could not see enough — read what it says it was
  missing, and decide whether the round covered the change at all.
"""


def render(data: dict, delivered: int, expected: int) -> str:
    c = data["counts"]
    out = [f"# Panel verdict — GO {c['GO']} · NO-GO {c['NO-GO']} · INSUFFICIENT {c['INSUFFICIENT']}"
           + (f" · no verdict {c['none']}" if c["none"] else ""),
           "",
           f"{len(data['findings'])} findings from {len(data['panelists'])} panelist(s).",
           ""]
    if data["blocking"]:
        out += [f"BLOCKING: {', '.join(data['blocking'])} — judge each against its cited "
                "code before landing.",
                "Clear one only by writing down what refutes it; unjudged means blocked.",
                ""]
    else:
        out += ["BLOCKING: none cited.", ""]
    if data["unsupported"]:
        out += [f"Unsupported NO-GO (citation unreadable): {', '.join(data['unsupported'])}",
                ""]
    if expected and delivered < expected:
        out += [f"> **WARNING:** only {delivered} of {expected} panelists delivered. This is a",
                "> partial panel — do not report it as a full one.", ""]
    out += [HOW_TO_READ, "## Verdicts", ""]

    for p in data["panelists"]:
        line = p["line"] or "_no VERDICT section_"
        if p["cites"]:
            line = re.sub(r"^NO-GO[^—]*—\s*[\d,\s/&]*(?:and)?\s*—?\s*",
                          f"NO-GO ({', '.join(p['cites'])}) — ", line)
        elif p["citation_unreadable"]:
            line += "  _(citation unreadable — read the file in full)_"
        out.append(f"- **{p['slug']}** — {line}")

    out += ["", "## Findings", ""]
    for f in data["findings"]:
        out.append(f"{f['id']}. {f['panelist']} — {f['text']}")
    quiet = [p["slug"] for p in data["panelists"] if not p["findings"]]
    if quiet:
        out += ["", f"No findings reported by: {', '.join(quiet)}"]
    return "\n".join(out) + "\n"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir", type=Path)
    ap.add_argument("round_dir", type=Path)
    ap.add_argument("--expected", type=int, default=0)
    ap.add_argument("--delivered", type=int, default=0)
    ap.add_argument("--min-decided", type=int, default=3)
    # Whatever the caller knows about WHAT was reviewed. Absent stays absent: an empty
    # string in a gate field is worse than no field, because it reads as an answer.
    ap.add_argument("--subject", action="append", default=[], metavar="KEY=VALUE")
    a = ap.parse_args(argv)

    data = build(a.round_dir)
    c = data["counts"]
    decided = c["GO"] + c["NO-GO"]

    (a.run_dir / "verdict.md").write_text(render(data, a.delivered, a.expected),
                                          encoding="utf-8")
    # written even when the round failed: why it failed is a fact a gate needs
    subject = dict(kv.split("=", 1) for kv in a.subject if "=" in kv and kv.split("=", 1)[1])
    (a.run_dir / "verdict.json").write_text(json.dumps({
        "run_id": a.run_dir.name,
        **({"subject": subject} if subject else {}),
        "quorum": {"expected": a.expected, "delivered": a.delivered,
                   "min_decided": a.min_decided, "decided": decided,
                   "met": decided >= a.min_decided},
        "counts": c,
        "blocking": data["blocking"],
        "unsupported_no_go": data["unsupported"],
        "panelists": [{"slug": p["slug"], "verdict": p["verdict"], "reason": p["line"],
                       "cites": p["cites"], "findings": len(p["findings"])}
                      for p in data["panelists"]],
        "findings": [{"id": f["id"], "panelist": f["panelist"],
                      "confidence": f["confidence"], "severity": f["severity"],
                      "text": f["text"]} for f in data["findings"]],
    }, indent=1), encoding="utf-8")

    print(f"{decided} decided ({c['GO']} GO, {c['NO-GO']} NO-GO, "
          f"{c['INSUFFICIENT']} INSUFFICIENT), {len(data['findings'])} findings")
    if decided < a.min_decided:
        print(f"only {decided} panelist(s) could form an opinion; "
              f"{c['INSUFFICIENT']} said INSUFFICIENT", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
