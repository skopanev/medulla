"""A NO-GO has to name what it rests on, or it cannot be acted on.

A landing went through on `GO 2 · NO-GO 2`: the reader saw a tie, read "verdicts are
not a vote", went to the findings, found nothing that passed the contract's blocker
test, and shipped. Formally correct — nothing tied the two NO-GO verdicts to anything
checkable. Now a NO-GO cites findings by their position in its own list, and the
collector turns those into the ids the reader acts on.
"""
import json
import subprocess
import sys
from pathlib import Path

COLLECTOR = Path(__file__).resolve().parent.parent / "workflows/spar/scripts/collect_verdict.py"


def collect(tmp_path, panelists, *, expected=None, delivered=None, min_decided=3):
    """Run the collector over a round, as the workflow node does."""
    art = tmp_path / "artifacts"
    art.mkdir(exist_ok=True)
    for entry in panelists:
        if len(entry) == 3:                      # (slug, findings, verdict)
            slug, findings, verdict = entry
            body = ("## FINDINGS\n" + ("\n".join(findings) if findings else "NONE")
                    + f"\n\n## VERDICT\n{verdict}\n")
        else:
            slug, body = entry
        (art / f"{slug}.md").write_text(body)
    n = len(panelists)
    res = subprocess.run(
        [sys.executable, str(COLLECTOR), str(tmp_path), str(art),
         "--expected", str(n if expected is None else expected),
         "--delivered", str(n if delivered is None else delivered),
         "--min-decided", str(min_decided)],
        capture_output=True, text=True, check=False)
    md = (tmp_path / "verdict.md").read_text() if (tmp_path / "verdict.md").exists() else ""
    data = (json.loads((tmp_path / "verdict.json").read_text())
            if (tmp_path / "verdict.json").exists() else {})
    return md, data, res


CACHE = "- (R) HIGH — cache key omits the tenant — src/cache.py:41 — cross-tenant read — FIX: add tenant_id in cache_key()"
NOISE = "- (G) LOW — noisy log — worker.py:12 — clutter — FIX: drop it"


def test_a_cited_no_go_becomes_a_blocking_id(tmp_path):
    out, _data, res = collect(tmp_path, [
        ("sonnet", [CACHE], "NO-GO — 1 — the cache defect leaks across tenants"),
        ("gemini", [NOISE], "GO — nothing blocking"),
    ])
    assert "BLOCKING: F1" in out
    assert "NO-GO (F1) — the cache defect" in out      # the local number is translated
    assert "BLOCKING" in out                        # and surfaced as an update signal


def test_an_unreadable_no_go_fails_closed(tmp_path):
    """A NO-GO whose citation cannot be read is still a NO-GO — a panelist objected.
    Printing "an opinion, not a block" and clearing the BLOCKING line hands an
    automated reader a green light built out of a parsing failure."""
    out, _data, _res = collect(tmp_path, [
        ("gpt5", [CACHE], "NO-GO — the change feels rushed"),
        ("gemini", [], "GO — fine"),
    ])
    assert "citation unreadable" in out
    assert "UNREAD-gpt5" in out
    assert "BLOCKING: none cited." not in out


def test_citations_are_read_however_they_are_written(tmp_path):
    """"F1, F2", "1 and 3", "findings 1/3" — the old character class stopped at the
    first unexpected character and dropped the rest silently."""
    out, _data, _res = collect(tmp_path, [
        ("sonnet", [CACHE, NOISE], "NO-GO — F1 and F2 — both matter"),
    ])
    blocking = next(l for l in out.splitlines() if l.startswith("BLOCKING:"))
    assert "F1" in blocking and "F2" in blocking


def test_severity_comes_from_the_slot_not_a_substring(tmp_path):
    """"(G) LOW — the HIGH watermark is cosmetic" sorted as HIGH under a substring
    match — the same mistake this repo fixed once when "agy" in a comment sent the
    runner into the Keychain."""
    out, _data, _res = collect(tmp_path, [
        ("sonnet", ["- (G) LOW — the HIGH watermark line is cosmetic — a.py:1 — noise — FIX: drop it",
                    "- (R) HIGH — real one — b.py:2 — breaks — FIX: guard it"],
         "GO — nothing blocking"),
    ])
    order = [l for l in out.splitlines() if l.startswith("F")]
    assert "real one" in order[0]          # the actual HIGH sorts first
    assert "watermark" in order[1]


def test_the_reported_tie_now_says_what_to_do(tmp_path):
    """GO 2 · NO-GO 2 — the exact shape that shipped."""
    out, _data, _res = collect(tmp_path, [
        ("sonnet", [CACHE, NOISE], "NO-GO — 1 — leaks across tenants"),
        ("gpt5", ["- (R) HIGH — no down migration — db/0142.sql — cannot roll back — FIX: write it"],
         "NO-GO — 1 — irreversible"),
        ("gemini", [], "GO — fine"),
        ("glm5", [], "GO — fine"),
    ])
    assert "GO 2 · NO-GO 2" in out
    blocking = next(l for l in out.splitlines() if l.startswith("BLOCKING:"))
    assert "F1" in blocking and "F2" in blocking       # both cited findings, whatever the order
    assert "NOT a vote" in out
    assert "not a draw to break by" in out


def test_findings_keep_their_ids_across_the_file(tmp_path):
    out, _data, _res = collect(tmp_path, [("sonnet", [CACHE, NOISE], "NO-GO — 2 — the log is noisy")])
    # the SECOND finding of sonnet is cited; severity sorting puts it last
    assert "BLOCKING: F2" in out
    assert "F2. sonnet — (G) LOW — noisy log" in out


def test_blocking_ids_are_sorted_and_deduplicated(tmp_path):
    """They arrive grouped by panelist, so the raw order reads F2, F24, F3, F5 — a
    list nobody can hold in their head. Seen on a live 44-finding round."""
    out, _data, _res = collect(tmp_path, [
        ("sonnet", [CACHE, NOISE], "NO-GO — 2, 1 — both matter"),
        ("gpt5", ["- (R) HIGH — no down migration — db/0142.sql — no rollback — FIX: write it"],
         "NO-GO — 1 — irreversible"),
    ])
    blocking = next(l for l in out.splitlines() if l.startswith("BLOCKING:"))
    ids = [int(t.strip(" F,")) for t in blocking.split(":")[1].split("—")[0].split(",")]
    assert ids == sorted(ids), blocking
    assert len(ids) == len(set(ids))
