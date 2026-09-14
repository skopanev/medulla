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
      "yellow": "\033[33m", "cyan": "\033[36m", "off": "\033[0m",
      # the palette an author's own signals are drawn from
      "blue": "\033[34m", "magenta": "\033[35m",
      "bcyan": "\033[96m", "bblue": "\033[94m", "bmagenta": "\033[95m"}

# Red, green and yellow are NOT here: they are the engine's own vocabulary, and a
# workflow's `RETRY` must never be able to borrow the colour that means failure.
#
# Twelve slots, not six: six hues plain, then the same six bold. A first cut with six ran
# out on a workflow with seven author signals and painted two of them alike — the exact
# failure this exists to prevent, found by running it rather than by reasoning about it.
# Bold reads as a different style, not a different hue, so the pairs stay distinguishable
# even on a terminal that renders bright colours poorly. Node names are bold and
# UNcoloured, so a bold-coloured signal is not confusable with one.
_HUES = ("cyan", "magenta", "blue", "bcyan", "bmagenta", "bblue")
_PALETTE = tuple((h,) for h in _HUES) + tuple((h, "bold") for h in _HUES)

# The class of a signal, for colour only. __default__ is yellow and not red on purpose:
# it is not a failure, it is a node that concluded without saying anything — which is
# its own thing to notice, and reading it as an error sends people hunting a crash.
_SIGNAL_COLOUR = {"__failed__": ("red",), "__exit_fail__": ("red",),
                  "__exit_ok__": ("green",), "__done__": ("green",),
                  "__default__": ("yellow",)}

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


_ASSIGNED: dict[str, tuple[str, ...]] = {}


def assign_signal_colours(signals) -> None:
    """Give every signal in THIS workflow its own colour. Called once, at run start.

    One name, one colour, and no two names sharing one — the same contract --graph
    already honours for its edges, and the same reason: three steps in a row that ended
    differently must not look alike. The engine's own vocabulary keeps its fixed colours
    (failure red, silence yellow, done green); everything else is the author's, and the
    engine grades none of it — `OK` and `READY` are just where the graph went next, so
    they get DIFFERENT colours rather than a shared one.

    The colour starts from a hash of the name, so a signal keeps its colour as a workflow
    grows and across runs, and steps to the next free slot on collision. Hashing alone is
    not enough — in this repository's own panel three different outcomes landed on one
    colour, which is the exact thing colouring was asked for to prevent.

    With more author signals than the palette holds, later names reuse a slot rather than
    fail: a repeat is still better than the one colour this replaced.
    """
    _ASSIGNED.clear()
    used = set()
    for sig in sorted(s for s in signals if s not in _SIGNAL_COLOUR):
        start = sum(sig.encode()) % len(_PALETTE)
        colour = _PALETTE[start]
        for step in range(len(_PALETTE)):
            candidate = _PALETTE[(start + step) % len(_PALETTE)]
            if candidate not in used:
                colour = candidate
                break
        used.add(colour)
        _ASSIGNED[sig] = colour


def signal_colour(signal: str) -> tuple[str, ...]:
    """The engine's own signals are fixed; an author's come from the run's assignment.

    Falls back to the hash directly for a signal never declared in on_signal — a pool's
    per-input signal, or a log line printed before assignment has happened."""
    if signal in _SIGNAL_COLOUR:
        return _SIGNAL_COLOUR[signal]
    if signal in _ASSIGNED:
        return _ASSIGNED[signal]
    return _PALETTE[sum(signal.encode()) % len(_PALETTE)]


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
    tgt = (paint(target, *signal_colour(target)) if target in _SIGNAL_COLOUR
           else paint(target, "bold"))
    return (f"step {step} | {paint(node, 'bold')} -> "
            f"{paint(signal, *signal_colour(signal))} -> "
            f"{tgt} {paint(f'({duration}s)', 'dim')}")
