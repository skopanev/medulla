"""How a run reads while it is running: the stamp, the colour, the step line.

Split from engine_scan.py under the project's 250-line rule ($MAX_LOC). engine_scan
turns output into facts; this decides how those facts LOOK, which is a different job
with a different way of going wrong — every escape written here can end up in somebody's
log file.

Every line used to be the same width, the same colour and undated, so a long run printed
one undifferentiated sheet: the reader could not see where a step began, which of them
had failed, or how long the whole thing had been going without doing arithmetic on
durations. The facts were all there; the shape hid them.

Colour is decoration and must never be the ONLY carrier of a fact — the words
__failed__ and __exit_ok__ still say it in full, so a pipe, a log file and a
colour-blind reader lose nothing.
"""
from __future__ import annotations

import os
import sys
import time

_C = {"dim": "\033[2m", "bold": "\033[1m", "red": "\033[31m", "green": "\033[32m",
      "yellow": "\033[33m", "cyan": "\033[36m", "off": "\033[0m"}

# The class of a signal, for colour only. __default__ is yellow and not red on purpose:
# it is not a failure, it is a node that concluded without saying anything — which is
# its own thing to notice, and reading it as an error sends people hunting a crash.
_SIGNAL_COLOUR = {"__failed__": "red", "__exit_fail__": "red",
                  "__exit_ok__": "green", "__done__": "green",
                  "__default__": "yellow"}

_T0 = time.monotonic()          # replaced by the engine when a run actually starts


def run_started_now() -> None:
    """Reset the elapsed clock. Called once when a run begins, so `+MM:SS` counts from
    the run and not from whenever this module happened to be imported."""
    global _T0
    _T0 = time.monotonic()


def colour_enabled() -> bool:
    """TTY only, and NO_COLOR wins. MEDULLA_COLOR=always|never overrides both — a panel
    runs inside a container with a pipe for stderr, where the honest default is off."""
    mode = os.environ.get("MEDULLA_COLOR", "").strip().lower()
    if mode in ("always", "1", "yes"):
        return True
    if mode in ("never", "0", "no"):
        return False
    if os.environ.get("NO_COLOR") is not None:
        return False
    return sys.stderr.isatty()


def paint(text: str, *names: str) -> str:
    if not text or not colour_enabled():
        return text
    return "".join(_C[n] for n in names) + text + _C["off"]


def signal_colour(signal: str) -> str:
    """Terminals and channel signals have fixed colours; anything the author named is
    cyan. A workflow's own vocabulary is not ours to grade — `OK` and `retry` are both
    just where the graph went next."""
    return _SIGNAL_COLOUR.get(signal, "cyan")


def _elapsed() -> str:
    """How long this run has been going. Minutes and seconds, hours only once there are
    hours — a panel at +03:12 and a migration at +2:41:07 both read at a glance."""
    secs = int(time.monotonic() - _T0)
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"+{h}:{m:02d}:{s:02d}" if h else f"+{m:02d}:{s:02d}"


def log(msg: str) -> None:
    stamp = paint(f"{time.strftime('%H:%M:%S')} {_elapsed()}", "dim")
    print(f"{paint('[medulla]', 'dim')} {stamp}  {msg}", file=sys.stderr)


def step_start(step: int, node: str) -> str:
    return f"step {step} | {paint(node, 'bold')}"


def step_end(step: int, node: str, signal: str, target: str, duration) -> str:
    """The line a reader scans for. The node is bold, the signal carries its class as
    colour, and the duration is dim — it is context, not the event."""
    # The target carries its class too when it is a terminal: how a run ENDED is the one
    # thing a reader scrolls to find, and `-> __exit_fail__` in plain bold looks exactly
    # like `-> next_node`.
    tgt = (paint(target, signal_colour(target)) if target in _SIGNAL_COLOUR
           else paint(target, "bold"))
    return (f"step {step} | {paint(node, 'bold')} -> "
            f"{paint(signal, signal_colour(signal))} -> "
            f"{tgt} {paint(f'({duration}s)', 'dim')}")
