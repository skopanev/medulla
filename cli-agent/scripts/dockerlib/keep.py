"""Containers that outlive one run, because the workflow named a session.

A conversation lives in the CLI's own state inside the container's $HOME. Copying that
state out is four different problems — claude keeps a jsonl file, opencode a SQLite
database, agy a database plus a protobuf — so the simple answer is not to throw the
container away: name it, reuse it while the pipeline runs, and remove it at the end.

The rule is deliberately blunt: a workflow that NAMES a session keeps its container.
No check for whether the session is actually resumed — that check is one more thing to
be wrong about, and a named-but-unused session costs one idle container for the length
of a run. Everything else keeps today's --rm.
"""
from __future__ import annotations

import os
import subprocess
import time

LABEL = "medulla.session-owner"
# Belt for the pipeline that never reached its own cleanup — a killed host, a lost
# terminal. A day, because medulla removes its containers on exit in all but the rare
# case, and a shorter sweep would start reaping containers of a pipeline still running.
STALE_AFTER_S = 24 * 3600


def container_name(owner: str) -> str:
    return f"medulla-sess-{owner}"


def owner_id() -> str:
    """Who the container belongs to: the OUTER run when there is one.

    MEDULLA_RUN_ID is forwarded into the container, so a nested `medulla --docker`
    launched from a host node inherits it — which is exactly the case this exists for.
    Its absence means we are the top of the pipeline and clean up after ourselves.
    """
    return os.environ.get("MEDULLA_RUN_ID", "")


def is_running(name: str) -> bool:
    try:
        out = subprocess.run(["docker", "ps", "-q", "--filter", f"name=^{name}$"],
                             capture_output=True, text=True, timeout=30, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return bool(out.strip())


def remove(name: str) -> None:
    subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)


def remove_for_owner(owner: str) -> int:
    """Remove every container this pipeline started. Called on the way out."""
    if not owner:
        return 0
    try:
        out = subprocess.run(["docker", "ps", "-aq", "--filter", f"label={LABEL}={owner}"],
                             capture_output=True, text=True, timeout=30, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return 0
    ids = [i for i in out.split() if i]
    for cid in ids:
        subprocess.run(["docker", "rm", "-f", cid], capture_output=True, check=False)
    return len(ids)


def sweep_stale(now: float | None = None) -> int:
    """Remove session containers older than a day, whoever owns them.

    Only ours — the label is the filter, and the daemon is shared with other people's
    work. Silent when there is nothing to do; a line per removal otherwise, because a
    container disappearing without explanation is its own kind of confusing.
    """
    now = time.time() if now is None else now
    try:
        out = subprocess.run(
            ["docker", "ps", "-a", "--filter", f"label={LABEL}",
             "--format", "{{.ID}}\t{{.CreatedAt}}\t{{.Names}}"],
            capture_output=True, text=True, timeout=30, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return 0
    removed = 0
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        cid, created, name = parts[0], parts[1], parts[2]
        age = _age_seconds(created, now)
        if age is not None and age > STALE_AFTER_S:
            subprocess.run(["docker", "rm", "-f", cid], capture_output=True, check=False)
            removed += 1
    return removed


def _age_seconds(created: str, now: float) -> float | None:
    """docker prints `2026-08-25 09:54:01 +0300 MSK` — parse the part that is standard."""
    import datetime
    try:
        stamp = datetime.datetime.strptime(" ".join(created.split()[:3]), "%Y-%m-%d %H:%M:%S %z")
    except (ValueError, IndexError):
        return None
    return now - stamp.timestamp()
