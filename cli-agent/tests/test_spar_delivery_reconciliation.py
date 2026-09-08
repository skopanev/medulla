"""Manifest delivery and artifact presence must agree before a file becomes an opinion."""
import json
import subprocess
import sys
from pathlib import Path

COLLECTOR = (Path(__file__).resolve().parent.parent
             / "workflows/spar/scripts/collect_verdict.py")
GOOD = """## FINDINGS
NONE

## VERDICT
GO — complete
<!-- spar-delivery-complete -->
"""
TRUNCATED = """## FINDINGS
NONE

## VERDICT
GO — t
"""


def _row(index, slug, ok, **extra):
    return {"index": index, "key": f"{index}:{slug}", "input": {"slug": slug},
            "ok": ok, "reason": "ok" if ok else "post", "rc": 0,
            "timed_out": False, "message": "" if ok else "delivery marker missing",
            **extra}


def _collect(tmp_path, files, rows, *, expected, min_decided=1):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    for slug, body in files.items():
        (artifacts / f"{slug}.md").write_text(body)
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text("".join(json.dumps(row) + "\n" for row in rows))
    result = subprocess.run(
        [sys.executable, str(COLLECTOR), str(tmp_path), str(artifacts),
         "--manifest", str(manifest), "--expected", str(expected),
         "--min-decided", str(min_decided)], capture_output=True, text=True,
        check=False)
    markdown = (tmp_path / "verdict.md").read_text()
    data = json.loads((tmp_path / "verdict.json").read_text())
    return markdown, data, result


def test_rejected_artifact_is_diagnostic_not_an_opinion(tmp_path):
    markdown, data, result = _collect(
        tmp_path, {"good": GOOD, "truncated": TRUNCATED},
        [_row(1, "good", True), _row(2, "truncated", False)], expected=2)

    assert result.returncode == 0
    assert [panelist["slug"] for panelist in data["panelists"]] == ["good"]
    assert data["quorum"]["delivered"] == 1
    assert data["delivery"]["accepted"] == ["good"]
    rejected = data["delivery"]["rejected_on_disk"][0]
    assert rejected == {"slug": "truncated", "index": 2, "key": "2:truncated",
                        "reason": "post", "rc": 0, "timed_out": False,
                        "message": "delivery marker missing", "artifact_verdict": "GO",
                        "artifact_malformed": [],
                        "size_bytes": len(TRUNCATED.encode())}
    assert data["delivery"]["has_mismatch"] is True
    assert data["delivery"]["review_required"] is False
    assert data["state"] == "CLEAR" and data["state_reasons"] == []
    assert "**DELIVERY MISMATCH:**" not in markdown and "`truncated.md`" in markdown
    assert "rejected artifact" in markdown and "parsed verdict=GO (excluded)" in markdown
    assert "does not require review" in markdown
    assert (tmp_path / "artifacts/truncated.md").read_text() == TRUNCATED


def test_latest_failure_excludes_an_older_success_for_the_same_input(tmp_path):
    rows = [_row(1, "panelist", True),
            _row(1, "panelist", False, message="latest post veto")]
    markdown, data, result = _collect(
        tmp_path, {"panelist": GOOD}, rows, expected=1)

    assert result.returncode == 3
    assert data["panelists"] == []
    assert data["quorum"]["delivered"] == 0
    assert data["delivery"]["rejected_on_disk"][0]["message"] == "latest post veto"
    assert "panelist" in markdown and "latest post veto" in markdown
    assert (tmp_path / "artifacts/panelist.md").is_file()


def test_both_directions_of_disagreement_are_machine_and_human_visible(tmp_path):
    markdown, data, result = _collect(
        tmp_path, {"orphan": GOOD}, [_row(1, "missing", True)], expected=1)

    assert result.returncode == 3
    assert data["delivery"]["manifest_only"] == [
        {"slug": "missing", "index": 1, "key": "1:missing"}]
    assert data["delivery"]["untracked_on_disk"] == ["orphan"]
    assert data["delivery"]["has_mismatch"] is True
    assert "missing" in markdown and "orphan" in markdown
    assert (tmp_path / "artifacts/orphan.md").is_file()


def test_slug_is_primary_when_manifest_keys_repeat(tmp_path):
    rows = [_row(1, "good", True), {**_row(1, "rejected", False), "key": "1:good"}]
    _markdown, data, result = _collect(
        tmp_path, {"good": GOOD, "rejected": TRUNCATED}, rows, expected=2)

    assert result.returncode == 0
    assert data["delivery"]["accepted"] == ["good"]
    assert data["delivery"]["rejected_on_disk"][0]["slug"] == "rejected"
    assert [panelist["slug"] for panelist in data["panelists"]] == ["good"]


def test_full_rejected_artifact_requires_review_even_when_quorum_is_met(tmp_path):
    full = TRUNCATED + "x" * 200
    files = {"a": GOOD, "b": GOOD, "c": GOOD, "rejected": full}
    rows = [_row(index, slug, True) for index, slug in enumerate(("a", "b", "c"), 1)]
    rows.append(_row(4, "rejected", False))
    markdown, data, result = _collect(
        tmp_path, files, rows, expected=4, min_decided=3)

    assert result.returncode == 0 and data["quorum"]["met"] is True
    assert data["state"] == "REVIEW_REQUIRED"
    assert data["state_reasons"] == ["delivery_mismatch"]
    assert data["delivery"]["review_required"] is True
    assert data["delivery"]["review_triggering_artifacts"] == [
        {"slug": "rejected", "kind": "rejected", "size_bytes": len(full.encode())}]
    assert "**DELIVERY MISMATCH:**" in markdown


def test_workflow_passes_manifest_and_groups_latest_rows_by_slug():
    workflow = (COLLECTOR.parent.parent / "workflow.yaml").read_text()
    assert '--manifest "$MEDULLA_MANIFEST_PANEL"' in workflow
    assert "group_by(.input.slug)" in workflow


def test_collector_refuses_to_fall_back_to_disk_only(tmp_path):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "orphan.md").write_text(GOOD)
    result = subprocess.run(
        [sys.executable, str(COLLECTOR), str(tmp_path), str(artifacts),
         "--expected", "1", "--min-decided", "1"], capture_output=True,
        text=True, check=False)

    assert result.returncode == 2 and "--manifest" in result.stderr
    assert not (tmp_path / "verdict.json").exists()


def test_non_object_manifest_row_fails_with_its_line_number(tmp_path):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text("[]\n")
    result = subprocess.run(
        [sys.executable, str(COLLECTOR), str(tmp_path), str(artifacts),
         "--manifest", str(manifest)], capture_output=True, text=True,
        check=False)

    assert result.returncode == 1
    assert "manifest line 1 is not an object" in result.stderr
