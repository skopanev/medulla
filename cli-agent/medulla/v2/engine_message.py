"""The human-facing sentence for how an attempt ended.

Split from engine_scan.py under the project's 250-line rule ($MAX_LOC). Scanning
answers "what did the process emit"; this answers "what do we tell the reader" —
and getting that wrong is expensive: a watchdog kill reported as a plain timeout
cost an hour of diagnosis on a live P0.
"""
from __future__ import annotations

from .model import SIG_DEFAULT, SIG_FAILED
from .engine_scan import _tail, scan_stdout

def conclusion_message(signal, action, result, total, limit_reason, fallback_used,
                       post_signal, post_scan, body_scan, known, *, agent_spec=None,
                       post_rc=None, post_stderr=""):
    """The human-facing sentence for how an attempt ended.

    Lives beside scan_stdout because every branch here is explaining what the scanner
    did or did not find, and because the retry loop was over the project's line limit
    carrying prose it does not otherwise need.
    """
    from .harness import resolve as resolve_harness
    if signal == SIG_FAILED:
        # A watchdog kill and a node timeout are both rc=124 — say which.
        if getattr(result, "killed_because", ""):
            return (f"body killed by the silence watchdog: {result.killed_because} "
                    f"({total} attempt(s)); stderr: {_tail(result.stderr)}")
        if post_rc and not result.rc and not result.timed_out:
            # Only when the body SURVIVED. When the body itself died, its stderr is
            # the evidence — a credential broker's stack trace, an OAuth prompt that
            # timed out — and the hook merely noticed the missing artifact afterwards.
            # Reporting the hook there would replace the cause with its symptom.
            # This branch is for the other case: the body delivered and exited 0, and
            # saying "body died: rc=0" is a contradiction that sent readers hunting a
            # crash that never happened while the artifact sat on disk, complete.
            return (f"post hook vetoed the attempt: rc={post_rc}, {total} attempt(s)"
                    f"{' (fallback tried)' if fallback_used else ''}; "
                    f"body rc={result.rc}; stderr: {_tail(post_stderr)}")
        if limit_reason:
            # Name the wall. "body died: rc=1" for an exhausted plan sent a
            # panel round down the wrong path more than once.
            message = (f"{limit_reason} ({total} attempt(s)"
                       f"{', fallback tried' if fallback_used else ''})")
        else:
            message = (f"body died: rc={result.rc}, {total} attempt(s)"
                       f"{' (fallback tried)' if fallback_used else ''}; "
                       f"stderr: {_tail(result.stderr)}")
        if action.kind == "agent":
            # harness-mined failure detail (codex error/turn.failed lives in
            # stdout JSON the signal filter rightly drops)
            detail = resolve_harness(agent_spec).extract_error(result.stdout)
            if detail:
                message += f"; {detail}"
    elif signal == SIG_DEFAULT:
        # "the node did its job, wrote its file, printed nothing, and failed"
        # is correct but startling — and it is only discoverable by reading the
        # journal. Name the rule in the message that reports it.
        message = ("no known signal emitted, so the node took __default__ "
                   "(route it, or print a signal, or the run fails here); "
                   f"stdout: {_tail(result.stdout)}")
        # A shell node whose signal sits mid-line used to be rescued by the
        # lenient parse, which is the same leniency that let an interpolated
        # value become a variable. Say what changed instead of leaving the
        # author with a node that "just stopped routing".
        if action.kind == "shell" and "<signal:" in result.stdout \
                and not scan_stdout(result.stdout, known).events == []:
            message += ("; note: a shell node's signal must start its own line "
                        "— a tag printed mid-line is treated as text")
    elif signal is None:
        message = ""                        # pool silent ok
    elif post_signal is not None and signal == post_signal:
        message = post_scan.first_body
    else:
        message = body_scan.first_body
    return message
