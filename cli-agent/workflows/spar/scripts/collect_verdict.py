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

from verdict_parse import NOT_PANELISTS, SEVERITY_ORDER, read_panelist


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
                unsupported.append(p["slug"])
                blocking.append(f"UNREAD-{p['slug']}")

    # An objection nobody can check does not VOTE, but it still BLOCKS. Counting it
    # as a NO-GO let a gate read "2 GO vs 2 NO-GO" from an opinion with nothing behind
    # it; dropping it outright would have been worse — the round would have gone CLEAR
    # with a non-empty blocking list. So: out of the arithmetic, into blocking, and
    # blocking alone is enough to hold the change (see `state` below).
    unsup = set(unsupported)
    counts = {w: sum(1 for p in panelists
                     if p["verdict"] == w and p["slug"] not in unsup)
              for w in ("GO", "NO-GO", "INSUFFICIENT")}
    counts["none"] = sum(1 for p in panelists if not p["verdict"])
    counts["unsupported"] = len(unsupported)
    # Unresolved (R) HIGH findings weigh the same as a NO-GO for a gate: a verified
    # defect at a cited line is what the contract's blocker test is about, whatever
    # verdict word the panelist chose around it.
    verified_high = [f["id"] for f in numbered
                     if f["severity"] == "HIGH" and f["confidence"] == "R"]
    return {
        "panelists": panelists,
        "findings": numbered,
        "blocking": sorted({b for b in blocking if b.startswith("F")},
                           key=lambda b: int(b[1:])) + sorted(
                               {b for b in blocking if not b.startswith("F")}),
        "unsupported": unsupported,
        "counts": counts,
        "verified_high": verified_high,
        "malformed": {p["slug"]: p["malformed"] for p in panelists if p["malformed"]},
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
    if data["malformed"]:
        out += ["Parsed with difficulty — read these files in full:"]
        out += [f"  {slug}: {'; '.join(why)}" for slug, why in data["malformed"].items()]
        out += [""]
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
    # ONE source of participant state. `--delivered` came from the manifest, which
    # records what the ENGINE concluded; the artifacts on disk record what actually
    # arrived. They disagreed — a post hook vetoed a complete file and the round
    # reported 3 delivered beside 4 decided. Whatever a gate reads, it now reads it
    # from the same files the verdicts were parsed from.
    delivered = len(data["panelists"])
    (a.run_dir / "verdict.md").write_text(render(data, delivered, a.expected),
                                          encoding="utf-8")
    # written even when the round failed: why it failed is a fact a gate needs
    subject = dict(kv.split("=", 1) for kv in a.subject if "=" in kv and kv.split("=", 1)[1])
    (a.run_dir / "verdict.json").write_text(json.dumps({
        "run_id": a.run_dir.name,
        **({"subject": subject} if subject else {}),
        "quorum": {"expected": a.expected, "delivered": delivered,
                   "min_decided": a.min_decided, "decided": decided,
                   "met": decided >= a.min_decided,
                   # kept only as a witness when the engine and the disk disagree
                   **({"manifest_delivered": a.delivered}
                      if a.delivered and a.delivered != delivered else {})},
        "counts": c,
        "blocking": data["blocking"],
        "unsupported_no_go": data["unsupported"],
        # One field a gate branches on. A NON-EMPTY blocking list is enough on its
        # own: an objection whose citation could not be read is out of the vote, so
        # without this clause the round would go CLEAR while still naming something
        # that holds it — fail-open, exactly backwards.
        "state": ("REVIEW_REQUIRED"
                  if (data["blocking"] or c["NO-GO"] or data["verified_high"]
                      or decided < a.min_decided)
                  else "CLEAR"),
        "verified_high": data["verified_high"],
        "parser": {"malformed": data["malformed"]},
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
