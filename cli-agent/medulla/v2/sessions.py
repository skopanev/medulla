"""Where a named conversation is remembered, and for how long.

Split from rundir.py under the project's 250-line rule ($MAX_LOC). A session outlives
the run that opened it — that is its whole purpose — so it needs a store the run does
not own: recorded per run for the record, per PIPELINE for the handoff, and removed
where the containers holding those conversations are removed.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


class SessionStore:
    """The session half of a RunStore, mixed in so callers see one object."""


    def session_entry(self, name: str) -> dict | None:
        """{"id": ..., "harness": ...} for a named conversation, or None on the first turn.

        Looked up in the run FIRST, then in the pipeline. The run's own copy is the
        record of what happened here; the pipeline's is what makes the handoff work at
        all — a develop unit opens a conversation, a host node starts the landing run,
        and that run is a NEW run directory with an empty store. It kept the container
        alive and then resumed nothing, which is the whole feature failing quietly.
        """
        for path in (self.dir / "sessions.json", _pipeline_sessions()):
            if path is None:
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            entry = data.get(name)
            if isinstance(entry, dict) and entry.get("id"):
                return entry
        return None

    def session_id_for(self, name: str) -> str | None:
        entry = self.session_entry(name)
        return entry.get("id") if entry else None

    def record_session(self, name: str, session_id: str, harness: str) -> None:
        """First writer wins.

        A pool of agents sharing one session name would otherwise have the last
        thread to finish decide which conversation the NEXT node continues — a
        coin flip between five panelists. Keeping the first is at least stable,
        and the log line below is what tells the author their name is not unique.
        """
        path = self.dir / "sessions.json"
        with self._session_lock:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = {}
            if name in data:
                if data[name].get("id") != session_id:
                    print(f"[medulla] session '{name}' already belongs to "
                          f"{data[name].get('id')}; keeping it (a second agent claimed "
                          f"{session_id} — give parallel agents distinct session names)",
                          file=sys.stderr, flush=True)
                return
            data[name] = {"id": session_id, "harness": harness}
            _write_json(path, data)
            # And to the pipeline, so a LATER run of the same pipeline finds it. Same
            # first-writer rule, written separately: a run's own file stays the record
            # of what that run did, whatever the pipeline accumulates around it.
            shared = _pipeline_sessions()
            if shared is not None:
                try:
                    have = json.loads(shared.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    have = {}
                if name not in have:
                    have[name] = {"id": session_id, "harness": harness}
                    _write_json(shared, have)


def _write_json(path: Path, data: dict) -> None:
    """Write whole or not at all: a reader can arrive mid-write."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    tmp.replace(path)

def _pipeline_sessions() -> Path | None:
    """Where conversations of THIS pipeline live, or None outside one.

    A pipeline is the outermost run (MEDULLA_PIPELINE_ID, inherited by every nested
    medulla). Under --docker the file lands in the container's $HOME, which is exactly
    where it needs to be: the container is kept alive for the same reason, so the store
    and the conversations it names live and die together.

    Native runs get the host's $HOME, and the file is small, named by run id, and
    swept with everything else older than a day.
    """
    pipeline = os.environ.get("MEDULLA_PIPELINE_ID", "").strip()
    if not pipeline or "/" in pipeline:
        return None
    return Path.home() / ".medulla" / "sessions" / f"{pipeline}.json"

def drop_pipeline_sessions(pipeline: str) -> None:
    """Remove THIS pipeline's session file.

    Called where its containers are removed: the ids inside name conversations that
    lived in those containers, so once they are gone the file is a list of handles to
    nothing. Leaving it for the day-sweep would leave a resume that silently opens a
    new conversation instead — worse than no file at all.
    """
    if not pipeline or "/" in pipeline:
        return
    try:
        (Path.home() / ".medulla" / "sessions" / f"{pipeline}.json").unlink()
    except OSError:
        pass

def sweep_pipeline_sessions(older_than_s: float = 24 * 3600) -> int:
    """Drop pipeline session files nothing can use any more.

    They name conversations inside a container that a finished pipeline has already
    removed, so keeping them is keeping ids that resolve to nothing. A day, matching
    the container sweep — the two describe the same thing from opposite ends.
    """
    import time
    root = Path.home() / ".medulla" / "sessions"
    if not root.is_dir():
        return 0
    cutoff = time.time() - older_than_s
    gone = 0
    for f in root.glob("*.json"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                gone += 1
        except OSError:
            continue
    return gone
