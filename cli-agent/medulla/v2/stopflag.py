"""One bit: a stop was ordered. Everything that is about to START work reads it.

Killing the live children is not a cancellation. A pool input runs in a worker
THREAD, and a signal handler raises KeyboardInterrupt in the MAIN thread only — so
the worker reads its killed child as an ordinary failed attempt and retries it.
Reported from a host that stops a run with SIGTERM to the process group: three
fanned-out critics each opened a NEW conversation inside the same second the stop
arrived, and the leader then sat in ThreadPoolExecutor.shutdown(wait=True) waiting
for the attempts it had just ordered. The launcher saw no exit inside its 10s grace
and sent SIGKILL, which tears outcome.json and leaves the run unresumable — the
opposite of what the graceful path exists for. A longer grace would only pay for
the extra calls.

Its own module because three layers read it and none of them may import the others:
the signal handler SETS it, the attempt loop and the pool refuse to start new work,
and procrun refuses to register a child that started after it.
"""
from __future__ import annotations

import threading

_STOPPING = threading.Event()


def request_stop() -> None:
    """Order a stop.

    Set this BEFORE any child is killed. Then every registration either precedes the
    handler's snapshot, and is killed, or sees the flag, and is refused. Setting it
    afterwards leaves a window where a child is neither.
    """
    _STOPPING.set()


def clear_stop() -> None:
    """A fresh run in this process begins unstopped (nested runs, the suite)."""
    _STOPPING.clear()


def stop_requested() -> bool:
    """A stop was ordered: start nothing new."""
    return _STOPPING.is_set()
