"""Exhaustive matrix for the pure classifier — the silent-failure surface (panel P2)."""
import pytest
from medulla.v2.classify import (
    AttemptDecision,
    Move,
    Verdict,
    classify_attempt,
    next_move,
)
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
