"""Pure outcome classification — no I/O, no threads, exhaustively testable.

Per-attempt (classify_attempt) and attempt-loop (next_move) decisions.
The rules encode the contract:
- known signal wins even over rc != 0
- post rc != 0  => attempt failed (retryable), regardless of the body
- post rc == 0 + signal => overrides the body's signal
- declared pool delivery confirmation beats a body timeout
- silence (rc 0, no known signal): agent — retry primary only, never fallback;
  shell — deterministic, not retried; exhausted => __default__
- rc != 0 / timeout: retry primary, then fallback (same attempts), then __failed__
- ignore_exit_code excuses rc only, never a timeout
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .model import SIG_DEFAULT, SIG_FAILED, SIG_TIMEOUT


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
    pool_mode: bool = False,
    delivery_confirmed: bool = False,
) -> AttemptDecision:
    if pool_mode and timed_out:
        if delivery_confirmed:
            return AttemptDecision(Verdict.SILENT)   # artifact truth beats body wall
        return AttemptDecision(Verdict.RETRY, failure_class="timeout")
    if post_rc is not None and post_rc != 0:
        # A body that did not survive did not fail its hook — it never finished for the
        # hook to judge. The hook then reports the only thing it can see, "no artifact",
        # and reporting THAT as the cause replaces the reason with a symptom. Resume,
        # diagnosis and every count of why rounds fail read this field.
        #
        # First measured for the wall: six archive rows said reason=post at
        # timed_out=True while their own message said `body died: rc=124`, against five
        # honestly recorded timeouts. That half was fixed; the other half was not, and
        # it is the larger one. Re-measured across 3104 stored attempts: of 369 rows
        # classed "post", 101 died with a non-zero rc — a provider refusing a stream, a
        # broker with no room, a CLI killed at -15 — while the artifact they were judged
        # for was never written. 27% of everything counted as a malformed answer was a
        # transport failure wearing its name.
        #
        # conclusion_message has drawn this line correctly all along (it says "body
        # died" whenever the body died), so the human-readable sentence and the
        # machine-readable class disagreed on the same attempt. The class now follows
        # the sentence. The post failure itself is not lost — it travels in message.
        if timed_out:
            return AttemptDecision(Verdict.RETRY, failure_class="timeout")
        if rc != 0:
            return AttemptDecision(Verdict.RETRY, failure_class="rc")
        return AttemptDecision(Verdict.RETRY, failure_class="post")
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
    timeout_routed: bool = False,   # the node named __timeout__ in on_signal
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
    # A step that ran out of time can say so — but ONLY to a node that asked. "did not
    # finish" and "finished badly" call for different handling (shrink the task versus
    # try another provider) and both used to arrive as __failed__, so an author could not
    # tell them apart in a route.
    #
    # The first cut emitted __timeout__ always and resolved its ROUTE back to __failed__
    # when unclaimed. Routing survived; the RECORD did not — the journal and outcome.json
    # started saying __timeout__ where every existing reader looks for __failed__, and an
    # old test caught it immediately. So the fact is offered, not imposed: a node that
    # names __timeout__ gets it, a node that does not sees exactly what it always saw.
    #
    # Nothing is hidden by that. The full reason is recorded either way — reason:
    # "timeout" in attempts.jsonl and the wall named in the step message. A signal is a
    # routing decision, and a route nobody declared must not rewrite the record.
    if decision.failure_class in ("timeout", "watchdog") and timeout_routed:
        return LoopMove(Move.DONE, SIG_TIMEOUT)
    return LoopMove(Move.DONE, SIG_FAILED)
