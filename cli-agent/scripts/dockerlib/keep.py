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
SPEC_LABEL = "medulla.session-spec"
# Belt for the pipeline that never reached its own cleanup — a killed host, a lost
# terminal. A day, because medulla removes its containers on exit in all but the rare
# case, and a shorter sweep would start reaping containers of a pipeline still running.
STALE_AFTER_S = 24 * 3600


def container_name(owner: str, spec: str = "") -> str:
    """One container per pipeline AND per execution spec.

    Keying on the pipeline alone let a later nested workflow land in a container built
    from another image, mounted on another workspace, or writable when it asked for
    --cwd-ro. The spec digest is what the container IS; the owner is whose it is.
    """
    return f"medulla-sess-{owner}-{spec}" if spec else f"medulla-sess-{owner}"


def spec_digest(image: str, volumes: list[str]) -> str:
    """A short hash of everything that decides what the container can see and do.

    Image and mounts, in the order they were built — a different order means a
    different view of the world, so it is not normalised away.
    """
    import hashlib
    payload = "\n".join([image, *volumes]).encode("utf-8", "surrogatepass")
    return hashlib.sha256(payload).hexdigest()[:8]


def owner_id() -> str:
    """Who the container belongs to: the PIPELINE, not the run.

    MEDULLA_RUN_ID is reset by every nested medulla — anchoring to it gives the
    landing run a different container than the unit run that opened the conversation,
    which is precisely the handoff this exists to serve. MEDULLA_PIPELINE_ID is set
    once by the outermost run and inherited by everything below it.

    Empty means nothing above us: we are the top and clean up after ourselves.
    """
    return os.environ.get("MEDULLA_PIPELINE_ID") or os.environ.get("MEDULLA_RUN_ID", "")


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
    _remove_dead()
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


def _remove_dead() -> int:
    """Remove session containers that are not RUNNING, whatever their age.

    A live one is always Up — it holds `sleep infinity` while the pipeline works, so
    Exited or Dead means nothing is waiting on it. `created` is deliberately NOT here:
    a container passes through that state while another process starts it, and reaping
    it there deletes a container out from under a worker about to use it.
    """
    # NOT status=created: that is the state a container passes through while another
    # process is starting it, and reaping it there deletes a container out from under
    # a worker that is about to use it. Exited and dead are finished, whoever started
    # them; a creation that genuinely stalled is left to the day-long sweep.
    try:
        out = subprocess.run(
            ["docker", "ps", "-aq", "--filter", f"label={LABEL}",
             "--filter", "status=exited", "--filter", "status=dead"],
            capture_output=True, text=True, timeout=30, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return 0
    ids = [i for i in out.split() if i]
    for cid in ids:
        subprocess.run(["docker", "rm", "-f", cid], capture_output=True, check=False)
    return len(ids)


def _age_seconds(created: str, now: float) -> float | None:
    """docker prints `2026-08-25 09:54:01 +0300 MSK` — parse the part that is standard."""
    import datetime
    try:
        stamp = datetime.datetime.strptime(" ".join(created.split()[:3]), "%Y-%m-%d %H:%M:%S %z")
    except (ValueError, IndexError):
        return None
    return now - stamp.timestamp()
