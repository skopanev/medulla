"""Intersect manifest conclusions with panelist files, preserving every disagreement."""
from __future__ import annotations

import json
from pathlib import Path

MIN_REVIEW_ARTIFACT_BYTES = 200


def _slug(row: dict) -> str | None:
    value = row.get("input")
    slug = value.get("slug") if isinstance(value, dict) else value
    return slug if isinstance(slug, str) and slug else None


def _diagnostic(slug: str, row: dict, path: Path) -> dict:
    return {"slug": slug, "index": row.get("index"), "key": row.get("key"),
            "reason": row.get("reason"), "rc": row.get("rc"),
            "timed_out": bool(row.get("timed_out")), "message": row.get("message") or "",
            "size_bytes": path.stat().st_size}


def _rows(manifest: Path) -> list[dict]:
    rows = []
    for number, line in enumerate(manifest.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"manifest line {number} is not JSON: {exc.msg}") from exc
        if not isinstance(row, dict):
            raise ValueError(f"manifest line {number} is not an object")
        rows.append(row)
    return rows


def reconcile(manifest: Path, round_dir: Path, excluded: set[str]) -> tuple[list[Path], dict]:
    """Return agreed files plus a machine-readable account of every mismatch."""
    files = {path.stem: path for path in sorted(round_dir.glob("*.md"))
             if path.name not in excluded}
    rows = _rows(manifest)
    latest: dict[str, dict] = {}
    unidentified = []
    for row in rows:
        slug = _slug(row)
        if slug is None:
            unidentified.append({"index": row.get("index"), "key": row.get("key"),
                                 "ok": bool(row.get("ok"))})
        else:
            # The artifact is named by slug. index/key repeat across real histories;
            # retain them as diagnostics, never as the join key.
            latest[slug] = row

    accepted, manifest_only, rejected = [], [], []
    for slug, row in sorted(latest.items()):
        if row.get("ok"):
            if slug in files:
                accepted.append(slug)
            else:
                manifest_only.append({"slug": slug, "index": row.get("index"),
                                      "key": row.get("key")})
        elif slug in files:
            rejected.append(_diagnostic(slug, row, files[slug]))

    untracked = sorted(set(files) - set(latest))
    disk_mismatches = ([{"slug": item["slug"], "kind": "rejected",
                         "size_bytes": item["size_bytes"]} for item in rejected]
                       + [{"slug": slug, "kind": "untracked",
                           "size_bytes": files[slug].stat().st_size} for slug in untracked])
    review_artifacts = [item for item in disk_mismatches
                        if item["size_bytes"] >= MIN_REVIEW_ARTIFACT_BYTES]
    ignored_fragments = [item for item in disk_mismatches
                         if item["size_bytes"] < MIN_REVIEW_ARTIFACT_BYTES]
    agreed_paths = [files[slug] for slug in accepted]
    mismatch = bool(manifest_only or rejected or untracked or unidentified)
    return agreed_paths, {
        "has_mismatch": mismatch,
        "review_required": bool(manifest_only or unidentified or review_artifacts),
        "accepted": accepted,
        "manifest_only": manifest_only,
        "rejected_on_disk": rejected,
        "untracked_on_disk": untracked,
        "unidentified_manifest_rows": unidentified,
        "review_triggering_artifacts": review_artifacts,
        "ignored_fragments": ignored_fragments,
        "manifest_delivered": sum(bool(row.get("ok")) for row in latest.values()),
        "disk_artifacts": len(files),
    }
