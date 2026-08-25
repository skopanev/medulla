"""`wait` reads the manifest, not the journal — and not the artifacts either.

It used to report "nothing has finished" while two panelists were done and their files
on disk. That was true of the JOURNAL, which is written when the whole node ends, and
false of the run: the engine appends a manifest row the moment each input concludes.
"""
import json
import subprocess
import sys
from pathlib import Path

READER = Path(__file__).resolve().parent.parent / "workflows/spar/scripts/panel_state.py"


def build(tmp_path, rows, expected):
    step = tmp_path / "steps" / "002-panel"
    step.mkdir(parents=True)
    (step / "inputs.json").write_text(json.dumps([{"slug": s} for s in expected]))
    (step / "manifest.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    return step


def read(tmp_path):
    res = subprocess.run([sys.executable, str(READER), str(tmp_path)],
                         capture_output=True, text=True, check=False)
    lines = res.stdout.splitlines()
    summary = next((l for l in lines if l.startswith("@@")), "")
    return [l for l in lines if not l.startswith("@@")], summary, res.returncode


def test_it_reports_who_delivered_and_who_is_still_out(tmp_path):
    build(tmp_path, [
        {"input": {"slug": "gemini"}, "ok": True, "duration_s": 547},
        {"input": {"slug": "gpt5"}, "ok": True, "duration_s": 1597},
        {"input": {"slug": "qwen"}, "ok": False, "reason": "pre",
         "message": "qwen sits this round out: HTTP 429; and more"},
    ], ["glm5", "gpt5", "sonnet", "gemini", "qwen"])
    lines, summary, rc = read(tmp_path)
    assert rc == 0
    assert summary == "@@ 2 5 glm5 sonnet"
    assert any("gemini" in l and "ok" in l and "9m" in l for l in lines)
    assert any("qwen" in l and "pre" in l and "429" in l for l in lines)
    assert any("glm5" in l and "running" in l for l in lines)
    assert all(";" not in l for l in lines)          # first clause only: this is a status line


def test_a_half_written_row_is_the_next_panelist_not_corruption(tmp_path):
    step = build(tmp_path, [{"input": {"slug": "gemini"}, "ok": True, "duration_s": 60}],
                 ["gemini", "gpt5"])
    with (step / "manifest.jsonl").open("a") as f:
        f.write('{"input": {"slug": "gpt5"}, "ok": tr')     # caught mid-append
    lines, summary, rc = read(tmp_path)
    assert rc == 0
    assert summary == "@@ 1 2 gpt5"                  # gpt5 counts as running, not failed


def test_a_pool_that_has_not_started_says_so_quietly(tmp_path):
    (tmp_path / "steps").mkdir()
    _lines, summary, rc = read(tmp_path)
    assert rc == 1 and summary == ""                 # the caller falls back to its own wording


def test_it_does_not_read_the_artifacts(tmp_path):
    """A file on disk cannot tell a delivery from something post rejected. In the live
    run sonnet wrote nothing and was correctly not counted, while gemini's 32-byte
    "NONE / GO" would pass any non-empty check."""
    build(tmp_path, [{"input": {"slug": "sonnet"}, "ok": False, "reason": "post",
                      "message": "no artifact"}], ["sonnet"])
    art = tmp_path / "artifacts"
    art.mkdir()
    (art / "sonnet.md").write_text("## FINDINGS\nNONE\n")   # present, and irrelevant
    _lines, summary, _rc = read(tmp_path)
    assert summary == "@@ 0 1 "
