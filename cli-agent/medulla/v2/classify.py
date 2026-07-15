"""Pure outcome classification — no I/O, no threads, exhaustively testable.

Per-attempt (classify_attempt) and attempt-loop (next_move) decisions.
The rules encode the contract:
- known signal wins even over rc != 0
- post rc != 0  => attempt failed (retryable), regardless of the body
- post rc == 0 + signal => overrides the body's signal
- silence (rc 0, no known signal): agent — retry primary only, never fallback;
  shell — deterministic, not retried; exhausted => __default__
- rc != 0 / timeout: retry primary, then fallback (same attempts), then __failed__
- ignore_exit_code excuses rc only, never a timeout
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .model import SIG_DEFAULT, SIG_FAILED


class Verdict(Enum):
    ROUTE = "route"      # a known signal decides
    SILENT = "silent"    # body finished ok but said nothing routable
    RETRY = "retry"      # attempt failed mechanically (rc!=0 / timeout / post failed)


@dataclass
class AttemptDecision:
    verdict: Verdict
    signal: str | None = None        # set for ROUTE
    failure_class: str | None = None  # for RETRY: "post" | "timeout" | "rc"


def classify_attempt(
    kind: str,                    # "shell" | "agent"
    rc: int,
    timed_out: bool,
    body_signal: str | None,      # first KNOWN signal from stdout, or None
    post_rc: int | None,          # None = no post hook
    post_signal: str | None,      # first KNOWN signal from post stdout, or None
    ignore_exit_code: bool,
) -> AttemptDecision:
    if post_rc is not None and post_rc != 0:
        return AttemptDecision(Verdict.RETRY, failure_class="post")   # post veto
    if post_rc == 0 and post_signal is not None:
        return AttemptDecision(Verdict.ROUTE, post_signal)   # post override
    if body_signal is not None:
        return AttemptDecision(Verdict.ROUTE, body_signal)   # signal beats rc
    if timed_out:
        return AttemptDecision(Verdict.RETRY, failure_class="timeout")
    if rc == 0 or ignore_exit_code:
        return AttemptDecision(Verdict.SILENT)
    return AttemptDecision(Verdict.RETRY, failure_class="rc")


class Move(Enum):
    DONE = "done"                    # node outcome decided: .signal
    RETRY_SAME = "retry_same"        # re-run current runner
    SWITCH_FALLBACK = "switch"       # move to fallback runner, attempt counter resets


@dataclass
class LoopMove:
    move: Move
    signal: str | None = None


def next_move(
    decision: AttemptDecision,
    kind: str,                 # of the CURRENT runner
    phase: str,                # "primary" | "fallback"
    attempt: int,              # 1-based, within the current phase
    max_attempts: int,
    has_fallback: bool,
    pool_mode: bool = False,
) -> LoopMove:
    if decision.verdict is Verdict.ROUTE:
        return LoopMove(Move.DONE, decision.signal)

    if decision.verdict is Verdict.SILENT:
        if pool_mode:
            # pool bodies write data, not signals (law of layers): silence at rc 0
            # is the normal successful outcome — DONE with no signal, no retries
            return LoopMove(Move.DONE, None)
        # decision nodes: silence is a failure to communicate — agent retries on
        # the PRIMARY only; shell silence is deterministic
        if kind == "agent" and phase == "primary" and attempt < max_attempts:
            return LoopMove(Move.RETRY_SAME)
        return LoopMove(Move.DONE, SIG_DEFAULT)

    # Verdict.RETRY — mechanical failure
    if attempt < max_attempts:
        return LoopMove(Move.RETRY_SAME)
    if phase == "primary" and has_fallback and kind == "agent":
        return LoopMove(Move.SWITCH_FALLBACK)
    return LoopMove(Move.DONE, SIG_FAILED)
