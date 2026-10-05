"""The agy prompt rides stdin, because argv has a hard ceiling.

`--print <text>` put the whole prompt in ONE argv string. Linux caps a single argument at
MAX_ARG_STRLEN = 131072 bytes, so a long prompt is not slow or expensive — it cannot be
executed at all. A planning round died on exactly that: argv[10] was 113694 bytes, the
engine's own guard stopped it before exec, and the manifest read reason=harness
attempts=0. A harness that never ran, for a prompt that was merely long.

Measured on agy 1.1.18 while fixing it: 113040 bytes through stdin returns status
SUCCESS, and --conversation still resumes (the model recalled a word from the first turn).
"""
import json

import pytest
from medulla.v2 import harness as H
from medulla.v2.model import AgentSpec


def _build(tmp_path, prompt="PROMPT", **kw):
    a = H.AgyAdapter.__new__(H.AgyAdapter)        # skip the binary/trust preflight
    return a.build(AgentSpec(harness="agy", **kw), tmp_path / "p.md", prompt, 600)


def test_the_prompt_is_not_in_argv(tmp_path):
    inv = _build(tmp_path, prompt="THE WHOLE TASK")
    assert "THE WHOLE TASK" not in " ".join(inv.argv)
    assert "--print" not in inv.argv, "--input-format already selects print mode"


def test_the_prompt_arrives_on_stdin_in_agys_own_envelope(tmp_path):
    """Keyed on "event", NOT the claude-compatible "type". agy rejects the latter with
    'stream input message is missing the "event" field' — measured, not assumed."""
    inv = _build(tmp_path, prompt="THE WHOLE TASK")
    assert inv.stdin.endswith("\n"), "one NDJSON message per line"
    msg = json.loads(inv.stdin)
    assert msg["event"] == "user"
    assert msg["message"]["content"][0]["text"] == "THE WHOLE TASK"


def test_stream_json_input_requires_the_output_format_it_already_had(tmp_path):
    """--input-format stream-json needs --output-format stream-json. The adapter asked
    for that already, which is why this transport costs nothing else — and if someone
    drops it, the input format breaks with it."""
    inv = _build(tmp_path)
    assert "--input-format" in inv.argv
    assert inv.argv[inv.argv.index("--input-format") + 1] == "stream-json"
    assert inv.argv[inv.argv.index("--output-format") + 1] == "stream-json"


def test_a_prompt_past_the_argv_ceiling_still_builds(tmp_path):
    """The failing size, pinned. 113694 bytes was the real one; MAX_ARG_STRLEN is 131072.
    No argv string may approach either."""
    big = "x" * 113694
    inv = _build(tmp_path, prompt=big)
    assert max(len(a) for a in inv.argv) < 1000
    assert len(inv.stdin) > 113694


def test_resume_still_goes_through_argv(tmp_path):
    """--conversation is small and belongs there. Verified live that it still resumes
    with a stdin prompt: the model recalled a word from the previous turn."""
    inv = _build(tmp_path, prompt="second turn")
    inv2 = H.AgyAdapter.__new__(H.AgyAdapter).build(
        AgentSpec(harness="agy"), tmp_path / "p.md", "second turn", 600, resume="conv-1")
    assert "--conversation" in inv2.argv
    assert inv2.argv[inv2.argv.index("--conversation") + 1] == "conv-1"
    assert json.loads(inv2.stdin)["message"]["content"][0]["text"] == "second turn"
    assert "--conversation" not in inv.argv


def test_the_engines_argv_guard_still_covers_this_harness(tmp_path):
    """The guard is what turned an E2BIG crash into a readable message. The fix must not
    remove it — a future flag could put something large back on argv."""
    from medulla.v2.errors import EngineCrash
    from medulla.v2.harness_base import Invoke
    with pytest.raises(EngineCrash, match="E2BIG"):
        Invoke(argv=["agy", "x" * 100_000])
