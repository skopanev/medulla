"""Run directory: runs/<ts>-<run_id>/ — journal, vars, steps, atomic outcome.

Phase-1 scope: single-process writes (a lock guards journal appends so pool workers
can share it later); flock and prune land with the CLI part.
"""
from __future__ import annotations

import datetime
import json
import os
import threading
import uuid
from pathlib import Path

import yaml


class RunStore:
    def __init__(self, run_dir: Path, run_id: str):
        self.dir = run_dir
        self.run_id = run_id
        self.steps_dir = run_dir / "steps"
        self._journal_lock = threading.Lock()
        self._step_counter = 0

    @classmethod
    def create(cls, pipeline_dir: Path, config_text: str, run_id: str | None = None) -> "RunStore":
        run_id = run_id or os.environ.get("MEDULLA_RUN_ID", "").strip() or uuid.uuid4().hex[:8]
        ts = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        run_dir = pipeline_dir / "runs" / f"{ts}-{run_id}"
        run_dir.mkdir(parents=True, exist_ok=False)
        (run_dir / "steps").mkdir()
        (run_dir / "pipeline.yaml").write_text(config_text, encoding="utf-8")  # immutable snapshot
        store = cls(run_dir, run_id)
        return store

    # ── steps ──
    def new_step_dir(self, node_name: str) -> tuple[int, Path]:
        self._step_counter += 1
        step_dir = self.steps_dir / f"{self._step_counter:03d}-{node_name}"
        step_dir.mkdir(parents=True, exist_ok=True)
        return self._step_counter, step_dir

    # ── journal: append-only, one row per completed step ──
    def journal_append(self, row: dict) -> None:
        row = {"ts": _now(), **row}
        line = json.dumps(row, ensure_ascii=False)
        with self._journal_lock:
            with open(self.dir / "journal.jsonl", "a", encoding="utf-8") as f:
                f.write(line + "\n")

    # ── pool manifest: crash-safe done-mask, one complete line per write ──
    def manifest_append(self, manifest_path: Path, row: dict) -> None:
        line = json.dumps(row, ensure_ascii=False)
        with self._journal_lock:
            with open(manifest_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")   # single write: a crash can only truncate the tail

    # ── vars ──
    def write_vars(self, vars_map: dict[str, str]) -> None:
        tmp = self.dir / ".vars.tmp"
        tmp.write_text(yaml.safe_dump(dict(vars_map), sort_keys=False), encoding="utf-8")
        tmp.replace(self.dir / "vars.yaml")

    # ── outcome: appears only on completion, atomically ──
    def write_outcome(self, outcome: dict) -> None:
        tmp = self.dir / ".outcome.tmp"
        tmp.write_text(json.dumps(outcome, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        tmp.replace(self.dir / "outcome.json")


def _now() -> str:
    return datetime.datetime.now().strftime("%H:%M:%S")
