"""Run directory: runs/<ts>-<run_id>/ — journal, vars, steps, atomic outcome.

Cross-process safety: a per-run flock (runs/<id>/.lock) makes each run single-
writer across processes; the threading lock orders pool workers inside that one
writer. The flock is held for process lifetime and dies with the process.
"""
from __future__ import annotations

import datetime
import fcntl
import json
import os
import shutil
import threading
import uuid
from pathlib import Path

import yaml


class RunLocked(Exception):
    """Another process holds this run's lock."""


class RunStore:
    def __init__(self, run_dir: Path, run_id: str):
        self.dir = run_dir
        self.run_id = run_id
        self.steps_dir = run_dir / "steps"
        self._journal_lock = threading.Lock()
        self._step_counter = 0
        self._lock_fd = None

    def _acquire_flock(self) -> None:
        fd = os.open(self.dir / ".lock", os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise RunLocked(f"run {self.run_id} is already in progress")
        os.write(fd, str(os.getpid()).encode())
        self._lock_fd = fd                     # held until close() or process death

    def close(self) -> None:
        if self._lock_fd is not None:
            try:
                os.close(self._lock_fd)        # releases the flock
            except OSError:
                pass
            self._lock_fd = None

    @classmethod
    def create(cls, pipeline_dir: Path, config_text: str, run_id: str | None = None) -> "RunStore":
        run_id = run_id or os.environ.get("MEDULLA_RUN_ID", "").strip() or uuid.uuid4().hex[:8]
        ts = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        run_dir = pipeline_dir / "runs" / f"{ts}-{run_id}"
        run_dir.mkdir(parents=True, exist_ok=False)
        (run_dir / "steps").mkdir()
        (run_dir / "pipeline.yaml").write_text(config_text, encoding="utf-8")  # immutable snapshot
        store = cls(run_dir, run_id)
        store._acquire_flock()
        return store

    @classmethod
    def open(cls, run_dir: Path) -> "RunStore":
        """Reopen an existing run for resume. run_id comes from the dir name."""
        run_dir = Path(run_dir)
        if not (run_dir / "pipeline.yaml").is_file():
            raise FileNotFoundError(f"not a run directory: {run_dir}")
        run_id = run_dir.name.rsplit("-", 1)[-1]
        store = cls(run_dir, run_id)
        store._acquire_flock()
        (run_dir / "steps").mkdir(exist_ok=True)
        return store

    # ── resume readers ──
    def read_journal(self) -> list[dict]:
        """Tolerates a truncated FINAL line (single-write append guarantees at most
        one); a broken non-final line is corruption and must be loud."""
        path = self.dir / "journal.jsonl"
        if not path.is_file():
            return []
        return _read_jsonl_tolerant(path, what="journal")

    def read_manifest(self, manifest_path: Path) -> list[dict]:
        if not Path(manifest_path).is_file():
            return []
        return _read_jsonl_tolerant(Path(manifest_path), what="manifest")

    def read_vars(self) -> dict[str, str] | None:
        path = self.dir / "vars.yaml"
        if not path.is_file():
            return None
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return {k: str(v) for k, v in (data or {}).items()}

    def set_step_counter(self, value: int) -> None:
        self._step_counter = value

    @property
    def started_at(self) -> datetime.datetime:
        """Boot time of the ORIGINAL invocation, parsed from the dir name —
        cumulative duration across resumes with zero extra files."""
        ts = self.dir.name.rsplit("-", 1)[0]
        return datetime.datetime.strptime(ts, "%Y-%m-%d_%H-%M-%S")

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


def _read_jsonl_tolerant(path: Path, what: str) -> list[dict]:
    lines = path.read_text(encoding="utf-8").splitlines()
    rows: list[dict] = []
    for n, line in enumerate(lines):
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            if n == len(lines) - 1:
                return rows                    # crash-truncated tail: drop, re-run covers it
            raise RuntimeError(f"corrupt {what} at {path}:{n + 1} (non-final line)")
    return rows


def prune_runs(pipeline_dir: Path, keep_runs: int, pipeline_timeout: int | None) -> None:
    """On boot, after the new run dir exists. Finished (has outcome.json): keep the
    newest keep_runs. Unfinished: never touch while younger than the pipeline
    timeout (the active-run shield); timeout 0/None = never auto-prune unfinished."""
    runs_dir = pipeline_dir / "runs"
    if not runs_dir.is_dir():
        return
    finished: list[Path] = []
    now = datetime.datetime.now()
    for run in runs_dir.iterdir():
        if not run.is_dir():
            continue
        if (run / "outcome.json").is_file():
            finished.append(run)
            continue
        if pipeline_timeout:
            try:
                ts = datetime.datetime.strptime(run.name.rsplit("-", 1)[0], "%Y-%m-%d_%H-%M-%S")
            except ValueError:
                continue                       # unrecognized name: leave it alone
            if (now - ts).total_seconds() > pipeline_timeout * 2:
                shutil.rmtree(run, ignore_errors=True)   # certainly dead: deadline long past
    for run in sorted(finished, key=lambda p: p.name, reverse=True)[keep_runs:]:
        shutil.rmtree(run, ignore_errors=True)
