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

# The version of THIS SCRIPT, not of the installed package. The first attempt asked
# importlib for "medulla" and got nothing: inside the panel container medulla lives in
# a pipx venv that the workflow's python3 cannot see, so the field never appeared in a
# real run — green in my venv, absent in production, which is the exact failure class
# this file has spent the night helping to find. And the package would have been the
# wrong answer anyway: the collector is a FILE from the mounted workflow directory,
# refreshed on the host independently of the engine in the container. That is why two
# rounds with one engine stamp can differ in fields. A test pins this to pyproject.
COLLECTOR_VERSION = "4.78.0"


def build(round_dir: Path, delivered_slugs=None) -> dict:
    panelists = [read_panelist(p) for p in sorted(round_dir.glob("*.md"))
                 if p.name not in NOT_PANELISTS]

    # A panelist the ENGINE refused is not a voter. The collector reads files off
    # disk, so an artifact rejected by the delivery hook still sat there with a
    # readable VERDICT and was counted — measured twice on live rounds, and the
    # second one had a clean parse and its findings intact, so this is not a
    # side-effect of a parsing failure but its own mechanism. One round read GO 2 /
    # NO-GO 2 where the engine's own manifest said GO 1 / NO-GO 2, and a landing was
    # standing on it. The text stays visible — a rejected artifact is still evidence
    # a human may want — but it does not vote and it does not block.
    refused = set()
    if delivered_slugs is not None:
        refused = {p["slug"] for p in panelists if p["slug"] not in delivered_slugs}
        for p in panelists:
            p["refused"] = p["slug"] in refused

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
        if p.get("refused"):
            p["cites"] = []
            continue
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
                     if p["verdict"] == w and p["slug"] not in unsup
                     and not p.get("refused"))
              for w in ("GO", "NO-GO", "INSUFFICIENT")}
    counts["none"] = sum(1 for p in panelists if not p["verdict"])
    counts["unsupported"] = len(unsupported)
    # Unresolved (R) HIGH findings weigh the same as a NO-GO for a gate: a verified
    # defect at a cited line is what the contract's blocker test is about, whatever
    # verdict word the panelist chose around it.
    verified_high = [f["id"] for f in numbered
                     if f["severity"] == "HIGH" and f["confidence"] == "R"
                     and f["panelist"] not in refused]
    # ...and therefore they belong in the WORK LIST, not only in the gate arithmetic.
    # They were counted in `state` and left out of `blocking`, so a verified HIGH that
    # no panelist happened to CITE in its one-line reason disappeared from the list
    # readers actually work from. Measured by a consumer across 14 rounds: 4 carried
    # at least one such finding, and in one of them it was a HIGH on the fault path —
    # fixed, but not because the list said so. Severity is the FINDER's call and HIGH
    # means "should stop the change", so an uncited one stops it too.
    # What CITATION alone would have blocked — so the promotion is visible in the
    # artifact instead of having to be inferred. A consumer noticed the gap: the
    # effect is invisible to `verified_high - blocking`, because making that
    # difference empty is exactly what promotion does. Seeing it needs a separate
    # count, and a reader should not have to keep one.
    cited = {b for b in blocking if b.startswith("F")}
    promoted = [f for f in verified_high if f not in cited]
    blocking.extend(verified_high)
    return {
        "panelists": panelists,
        "findings": numbered,
        "blocking": sorted({b for b in blocking if b.startswith("F")},
                           key=lambda b: int(b[1:])) + sorted(
                               {b for b in blocking if not b.startswith("F")}),
        "unsupported": unsupported,
        "counts": counts,
        "verified_high": verified_high,
        "promoted_high": promoted,
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


def _absent_seats(manifest_json, seated: set, expected: int, delivered: int) -> list:
    """Seats the round expected and did not get, each with why — the engine's word.

    Two kinds, and both are losses a reader must see. A seat with a manifest row
    failed somewhere the engine could name (`pre`, `watchdog`, `rc`). A seat with no
    row at all — a cancelled queue writes none by design — can only be counted, so it
    is reported as an unnamed absence rather than quietly dropped, because dropping
    it is what turns "3 of 4" into a clean "3 of 3".
    """
    seats = []
    try:
        rows = json.loads(manifest_json) if manifest_json else []
    except (ValueError, TypeError):
        rows = []                      # a malformed hand-off must not lose the verdict
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict) or r.get("ok"):
            continue
        slug = ((r.get("input") or {}).get("slug") if isinstance(r.get("input"), dict)
                else None) or r.get("key") or "unknown"
        if slug in seated:
            continue                   # it delivered; refused_by_engine covers that case
        seats.append({"slug": slug,
                      **({"reason": r["reason"]} if r.get("reason") else {}),
                      **({"attempts": r["attempts"]} if r.get("attempts") is not None else {}),
                      **({"message": str(r["message"])[:400]} if r.get("message") else {})})
    unnamed = (expected or 0) - delivered - len(seats)
    seats += [{"slug": "unknown", "reason": "no manifest row"}] * max(0, unnamed)
    return seats


def render(data: dict, delivered: int, expected: int, absent=()) -> str:
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
                "> partial panel — do not report it as a full one."]
        out += [f">   {s['slug']} — {s.get('message') or s.get('reason') or 'no reason recorded'}"
                for s in absent]
        out += [""]
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
    # Comma-separated slugs the ENGINE accepted. Absent: every artifact on disk
    # counts, which is the pre-4.70 behaviour and what old callers still get.
    ap.add_argument("--delivered-slugs", default=None)
    # The engine's own manifest rows, projected to what a reader needs. Used for ONE
    # thing: naming the seats that produced no artifact. Never for counting — the
    # artifacts on disk remain the single source of participant state.
    ap.add_argument("--manifest-json", default=None)
    # Whatever the caller knows about WHAT was reviewed. Absent stays absent: an empty
    # string in a gate field is worse than no field, because it reads as an answer.
    ap.add_argument("--subject", action="append", default=[], metavar="KEY=VALUE")
    a = ap.parse_args(argv)

    slugs = None
    if a.delivered_slugs is not None:
        slugs = {x.strip() for x in a.delivered_slugs.split(",") if x.strip()}
    data = build(a.round_dir, slugs)
    c = data["counts"]
    decided = c["GO"] + c["NO-GO"]
    # ONE source of participant state. `--delivered` came from the manifest, which
    # records what the ENGINE concluded; the artifacts on disk record what actually
    # arrived. They disagreed — a post hook vetoed a complete file and the round
    # reported 3 delivered beside 4 decided. Whatever a gate reads, it now reads it
    # from the same files the verdicts were parsed from.
    delivered = len(data["panelists"])
    # A SEAT THAT NEVER SAT LEAVES NO ROW, and a round of three then reads as a
    # unanimous three. Measured live: a pre hook refused one panelist on an HTTP 000
    # from its provider, attempts=0, no artifact, no entry under panelists — every
    # field consistent, arithmetic sound, and the only trace was expected=4 beside
    # delivered=3, two numbers a reader has to think to compare. The human channel
    # already warned; the machine channel said nothing, so tooling that trusted the
    # panelist list reported a full panel. Name the empty seats instead.
    absent = _absent_seats(a.manifest_json, {p["slug"] for p in data["panelists"]},
                           a.expected, delivered)
    (a.run_dir / "verdict.md").write_text(render(data, delivered, a.expected, absent),
                                          encoding="utf-8")
    # written even when the round failed: why it failed is a fact a gate needs
    subject = dict(kv.split("=", 1) for kv in a.subject if "=" in kv and kv.split("=", 1)[1])
    # Which engine computed THIS file. It was recorded only in source.txt beside the
    # verdict, so a stored round could not say for itself what the collector of the
    # day did or did not do — a reader comparing two rounds had to remember which
    # version gained which behaviour. A replay never rewrites a stored verdict (and
    # must not), so the number in an old file stays as it was computed; the stamp is
    # what lets a reader tell that from a current one.
    # TWO CLOCKS IN ONE FILE, and they can disagree. `engine` dates the ROUND — it
    # comes from source.txt, written when the run started. The COLLECTOR that wrote
    # this verdict may be newer: a round begun before an upgrade and synthesised after
    # it carries old engine with new fields. Measured in the field: verdicts stamped
    # engine 4.70.1 appear on BOTH sides of a field's introduction, monotonic by write
    # time and not by engine. Saying "read this field on 4.71.1+" would tell a reader
    # to ignore it exactly where it exists.
    collector = COLLECTOR_VERSION

    engine = ""
    try:
        src = (a.run_dir / "source.txt").read_text(encoding="utf-8")
        for line in src.splitlines():
            if line.startswith("engine:"):
                engine = line.split(":", 1)[1].strip()
                break
    except OSError:
        pass

    (a.run_dir / "verdict.json").write_text(json.dumps({
        "run_id": a.run_dir.name,
        **({"engine": engine} if engine else {}),
        **({"collector": collector} if collector else {}),
        **({"subject": subject} if subject else {}),
        "quorum": {"expected": a.expected, "delivered": delivered,
                   "min_decided": a.min_decided, "decided": decided,
                   "met": decided >= a.min_decided,
                   # kept only as a witness when the engine and the disk disagree
                   **({"manifest_delivered": a.delivered}
                      if a.delivered and a.delivered != delivered else {})},
        "counts": c,
        # who was expected and did not appear, with the engine's reason
        **({"absent": absent} if absent else {}),
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
        # verified HIGHs nobody cited, blocking on their own merit
        "promoted_high": data["promoted_high"],
        "parser": {"malformed": data["malformed"]},
        # coverage: what that panelist says it swept and what it did NOT reach. The
        # prompt has demanded it, and nothing carried it — so the one claim that
        # separates "nothing else is wrong" from "I did not look" reached no reader.
        "panelists": [{"slug": p["slug"], "verdict": p["verdict"], "reason": p["line"],
                       "cites": p["cites"], "findings": len(p["findings"]),
                       **({"coverage": p["coverage"]} if p.get("coverage") else {}),
                       **({"refused_by_engine": True} if p.get("refused") else {})}
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
