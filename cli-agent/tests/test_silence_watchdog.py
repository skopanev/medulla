"""The watchdog that killed a working panel.

A Finik round ended 0/5: four models from four vendors, all rc=124, all at
*identical* 1158s — two attempts of ~279s of output followed by exactly 300s of
silence. They were mid-generation (190 KB of events in one case), not hung. The
threshold was too low for real deliberation, and the manifest could not say a
watchdog had done it: `rc=124` reads the same as an honest node timeout.
"""
import os

from medulla.v2 import procrun


def _run(script, timeout_s, idle, cwd):
    env = {**os.environ, "MEDULLA_IDLE_OUTPUT_S": str(idle)}
    old = os.environ.get("MEDULLA_IDLE_OUTPUT_S")
    os.environ["MEDULLA_IDLE_OUTPUT_S"] = str(idle)
    try:
        import importlib
        importlib.reload(procrun)
        return procrun.run(["bash", "-c", script], cwd=cwd, timeout_s=timeout_s,
                           watch_output=True, extra_env=env)
    finally:
        if old is None:
            os.environ.pop("MEDULLA_IDLE_OUTPUT_S", None)
        else:
            os.environ["MEDULLA_IDLE_OUTPUT_S"] = old
        import importlib
        importlib.reload(procrun)


def test_a_talking_then_silent_agent_is_killed_and_says_why(tmp_path):
    """Talks, then goes quiet past the threshold — exactly the live shape."""
    res = _run("echo one; echo two; sleep 30", timeout_s=200, idle=3, cwd=tmp_path)
    assert res.timed_out
    assert res.rc == 124
    assert "silent for 3s" in res.killed_because, res.killed_because
    assert "2 lines" in res.killed_because, "say how much it had produced"


def test_an_agent_that_never_speaks_is_named_differently(tmp_path):
    """Silent from the start is a different failure from silent mid-work."""
    old = os.environ.get("MEDULLA_FIRST_OUTPUT_S")
    os.environ["MEDULLA_FIRST_OUTPUT_S"] = "2"
    try:
        import importlib
        importlib.reload(procrun)
        res = procrun.run(["bash", "-c", "sleep 30"], cwd=tmp_path, timeout_s=200,
                          watch_output=True)
    finally:
        if old is None:
            os.environ.pop("MEDULLA_FIRST_OUTPUT_S", None)
        else:
            os.environ["MEDULLA_FIRST_OUTPUT_S"] = old
        import importlib
        importlib.reload(procrun)
    assert res.timed_out
    assert "no output at all" in res.killed_because


def test_a_steadily_working_agent_survives(tmp_path):
    """The point of raising the threshold: deliberation is not death."""
    res = _run("for i in 1 2 3; do echo tick; sleep 1; done", timeout_s=200, idle=3, cwd=tmp_path)
    assert not res.timed_out
    assert res.rc == 0
    assert res.killed_because == ""


def test_an_honest_timeout_carries_no_watchdog_reason(tmp_path):
    """rc=124 alone must stay distinguishable from a watchdog kill."""
    res = procrun.run(["bash", "-c", "while :; do echo tick; sleep 0.1; done"],
                      cwd=tmp_path, timeout_s=2, watch_output=False)
    assert res.timed_out and res.rc == 124
    assert res.killed_because == "", "no watchdog ran; the field must stay empty"


def test_the_default_threshold_is_no_longer_300():
    """300 was measured wrong against real work; 900 still catches the 10-14
    minute silences the watchdog exists for."""
    assert procrun.IDLE_OUTPUT_S == 900


def test_a_bad_override_falls_back_to_the_default():
    for junk in ("0", "-5", "abc", ""):
        os.environ["MEDULLA_IDLE_OUTPUT_S"] = junk
        try:
            import importlib
            importlib.reload(procrun)
            assert procrun.IDLE_OUTPUT_S == 900, junk
        finally:
            os.environ.pop("MEDULLA_IDLE_OUTPUT_S", None)
            import importlib
            importlib.reload(procrun)
