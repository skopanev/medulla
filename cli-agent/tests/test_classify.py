"""Exhaustive matrix for the pure classifier — the silent-failure surface (panel P2)."""
import pytest
from medulla.v2.classify import (
    AttemptDecision,
    Move,
    Verdict,
    classify_attempt,
    next_move,
)
from medulla.v2.engine_scan import ScanResult
from medulla.v2.model import SIG_DEFAULT, SIG_FAILED

# ── classify_attempt ─────────────────────────────────────────────────────────

def C(kind="agent", rc=0, timed_out=False, body_signal=None,
      post_rc=None, post_signal=None, ignore_exit_code=False):
    return classify_attempt(kind, rc, timed_out, body_signal, post_rc, post_signal, ignore_exit_code)


def test_signal_routes_on_success():
    d = C(rc=0, body_signal="planned")
    assert d.verdict is Verdict.ROUTE and d.signal == "planned"


def test_signal_beats_nonzero_rc():
    d = C(rc=3, body_signal="planned")
    assert d.verdict is Verdict.ROUTE and d.signal == "planned"


def test_signal_beats_timeout():
    # the body emitted the signal before being killed — the main thing was said
    d = C(rc=124, timed_out=True, body_signal="planned")
    assert d.verdict is Verdict.ROUTE


def test_silence_on_success():
    assert C(rc=0).verdict is Verdict.SILENT


def test_nonzero_rc_retries():
    assert C(rc=1).verdict is Verdict.RETRY


def test_timeout_retries():
    assert C(rc=124, timed_out=True).verdict is Verdict.RETRY


def test_ignore_exit_code_excuses_rc():
    assert C(rc=1, ignore_exit_code=True).verdict is Verdict.SILENT


def test_ignore_exit_code_never_excuses_timeout():
    assert C(rc=124, timed_out=True, ignore_exit_code=True).verdict is Verdict.RETRY


def test_post_veto_beats_body_signal():
    # post rc != 0 => attempt failed, even though the body emitted a signal
    d = C(rc=0, body_signal="planned", post_rc=1)
    assert d.verdict is Verdict.RETRY


def test_post_override_beats_body_signal():
    d = C(rc=0, body_signal="planned", post_rc=0, post_signal="needs_rework")
    assert d.verdict is Verdict.ROUTE and d.signal == "needs_rework"


def test_post_silent_keeps_body_signal():
    d = C(rc=0, body_signal="planned", post_rc=0)
    assert d.verdict is Verdict.ROUTE and d.signal == "planned"


def test_post_pass_body_silent():
    assert C(rc=0, post_rc=0).verdict is Verdict.SILENT


def test_post_veto_on_dead_body():
    assert C(rc=1, post_rc=1).verdict is Verdict.RETRY


# ── next_move ────────────────────────────────────────────────────────────────

ROUTE = AttemptDecision(Verdict.ROUTE, "ok")
SILENT = AttemptDecision(Verdict.SILENT)
RETRY = AttemptDecision(Verdict.RETRY)


def test_route_is_done():
    m = next_move(ROUTE, "agent", "primary", 1, 3, True)
    assert m.move is Move.DONE and m.signal == "ok"


def test_shell_silence_never_retried():
    m = next_move(SILENT, "shell", "primary", 1, 3, False)
    assert m.move is Move.DONE and m.signal == SIG_DEFAULT


def test_agent_silence_retries_on_primary():
    m = next_move(SILENT, "agent", "primary", 1, 2, True)
    assert m.move is Move.RETRY_SAME


def test_agent_silence_exhausted_is_default_never_fallback():
    m = next_move(SILENT, "agent", "primary", 2, 2, True)  # fallback available but NOT taken
    assert m.move is Move.DONE and m.signal == SIG_DEFAULT


def test_agent_silence_on_fallback_never_retried():
    m = next_move(SILENT, "agent", "fallback", 1, 3, False)
    assert m.move is Move.DONE and m.signal == SIG_DEFAULT


def test_mechanical_failure_retries_within_attempts():
    m = next_move(RETRY, "agent", "primary", 1, 2, True)
    assert m.move is Move.RETRY_SAME


def test_mechanical_failure_switches_to_fallback():
    m = next_move(RETRY, "agent", "primary", 2, 2, True)
    assert m.move is Move.SWITCH_FALLBACK


def test_mechanical_failure_no_fallback_is_failed():
    m = next_move(RETRY, "agent", "primary", 2, 2, False)
    assert m.move is Move.DONE and m.signal == SIG_FAILED


def test_fallback_exhausted_is_failed():
    m = next_move(RETRY, "agent", "fallback", 2, 2, False)
    assert m.move is Move.DONE and m.signal == SIG_FAILED


def test_shell_never_switches_to_fallback():
    m = next_move(RETRY, "shell", "primary", 2, 2, True)
    assert m.move is Move.DONE and m.signal == SIG_FAILED


@pytest.mark.parametrize("attempt,max_attempts,expect", [
    (1, 3, Move.RETRY_SAME), (2, 3, Move.RETRY_SAME), (3, 3, Move.SWITCH_FALLBACK),
])
def test_attempt_budget_boundaries(attempt, max_attempts, expect):
    m = next_move(RETRY, "agent", "primary", attempt, max_attempts, True)
    assert m.move is expect


# ── the body's cause of death outranks the hook's veto (medulla-1bzrxn2wwl) ──

def test_a_wall_clock_kill_is_not_reported_as_a_post_veto():
    """Measured across the archive: 704 manifests, 3465 rows, SIX inputs written
    as reason=post while timed_out=True and the message beside them said, word for
    word, `body died: rc=124`. For scale, honestly-recorded timeouts number five —
    the masking was MORE common than the correct record. Any aggregation by reason
    reads a false picture, including the one used to measure a previous fix."""
    d = classify_attempt("agent", 124, True, None, 1, None, False)
    assert d.verdict is Verdict.RETRY
    assert d.failure_class == "timeout", d.failure_class


def test_a_post_veto_without_a_timeout_is_still_a_post_veto():
    d = classify_attempt("agent", 0, False, None, 1, None, False)
    assert d.verdict is Verdict.RETRY and d.failure_class == "post"


# ── a dead body is not a rejected answer ────────────────────────────────────
#
# The post hook reports the only thing it can see when a body dies: no artifact. Counted
# as the CAUSE, that turns every provider refusal, broker outage and killed CLI into a
# malformed answer. Measured across 3104 stored attempts: of 369 rows classed "post",
# 101 had a non-zero rc and no timeout — 27% of everything counted as a bad answer was a
# transport failure wearing its name. Reported from the field as two review rounds lost
# to "stream disconnected before completion", filed against format rejection.
#
# conclusion_message has always drawn this line correctly ("body died: rc=..."), so the
# sentence a human read and the class a tool counted disagreed about the same attempt.

def _classify(rc=0, timed_out=False, post_rc=1):
    return classify_attempt(kind="agent", rc=rc, timed_out=timed_out, body_signal=None,
                            post_rc=post_rc, post_signal=None, ignore_exit_code=False,
                            pool_mode=True)


def test_a_body_that_died_is_classed_by_its_own_failure():
    """codex exiting 1 on "stream disconnected" is an rc failure, not a veto."""
    assert _classify(rc=1).failure_class == "rc"


def test_a_body_killed_by_a_signal_is_not_a_veto_either():
    """-15 and 143 are both SIGTERM; 24 archive rows carried them under reason=post."""
    for rc in (-15, 143, 2):
        assert _classify(rc=rc).failure_class == "rc", rc


def test_a_body_killed_by_the_wall_is_still_a_timeout():
    """The half that was already fixed, kept fixed: the wall wins over the hook."""
    assert _classify(rc=124, timed_out=True).failure_class == "timeout"


def test_a_veto_of_a_SURVIVING_body_is_still_a_veto():
    """The 268 honest rows. Narrowing the class must not empty it — a delivered answer
    that the hook refused for its format is exactly what "post" is for."""
    assert _classify(rc=0).failure_class == "post"


def test_the_class_agrees_with_the_sentence_a_human_reads():
    """The invariant behind all of the above: conclusion_message says "post hook vetoed"
    only when the body survived (rc=0, no timeout). The class must draw the same line,
    or the archive counts one thing while the manifest says another."""
    from medulla.v2.engine_message import conclusion_message

    class R:
        def __init__(self, rc, timed_out):
            self.rc, self.timed_out, self.stderr = rc, timed_out, "boom"
            self.killed_because = ""
            self.stdout = ""

    class Act:
        # shell, so the sentence builder does not go mining a harness for error detail —
        # the branch under test (veto vs died) is decided before kind is consulted.
        kind = "shell"

    for rc, timed_out in ((1, False), (-15, False), (124, True), (0, False)):
        decision = _classify(rc=rc, timed_out=timed_out)
        sentence = conclusion_message(
            "__failed__", Act(), R(rc, timed_out), 1, None, False, None,
            ScanResult(), ScanResult(), set(), post_rc=1, post_stderr="no artifact")
        says_veto = "post hook vetoed" in sentence
        assert says_veto == (decision.failure_class == "post"), (rc, timed_out, sentence)
