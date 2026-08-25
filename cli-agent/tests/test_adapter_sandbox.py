"""sandbox: read-only means a different flag to every CLI.

plan mode, -s read-only, --mode plan. The mapping is only true if the flag actually
appears in argv, so each is asserted by name.
"""
import json
import os
import stat
from pathlib import Path

import pytest
from conftest import write
from medulla.v2 import harness as H
from medulla.v2.errors import EngineCrash
from medulla.v2.model import AgentSpec


def test_claude_sandbox_read_only_is_plan_mode(tmp_path):
    a = H.ClaudeAdapter.__new__(H.ClaudeAdapter)
    inv = a.build(AgentSpec(harness="claude-code", sandbox="read-only"),
                  tmp_path / "p.md", "P", 60)
    i = inv.argv.index("--permission-mode")
    assert inv.argv[i + 1] == "plan"
    assert "--dangerously-skip-permissions" not in inv.argv


def test_claude_sandbox_danger_is_skip_permissions(tmp_path):
    a = H.ClaudeAdapter.__new__(H.ClaudeAdapter)
    for spec in (AgentSpec(harness="claude-code"),                      # default
                 AgentSpec(harness="claude-code", sandbox="danger")):
        inv = a.build(spec, tmp_path / "p.md", "P", 60)
        assert "--dangerously-skip-permissions" in inv.argv
        assert "--permission-mode" not in inv.argv


def test_codex_sandbox_read_only(tmp_path):
    a = H.CodexAdapter.__new__(H.CodexAdapter)
    inv = a.build(AgentSpec(harness="codex", sandbox="read-only"),
                  tmp_path / "p.md", "P", 60)
    i = inv.argv.index("-s")
    assert inv.argv[i + 1] == "read-only"
    assert "--dangerously-bypass-approvals-and-sandbox" not in inv.argv


def test_opencode_sandbox_read_only_denies_writes(tmp_path):
    # opencode has no read-only flag — the config is the only lever. bash is
    # denied too: a shell is a write primitive.
    a = H.OpenCodeAdapter.__new__(H.OpenCodeAdapter)
    inv = a.build(AgentSpec(harness="opencode", model="zai/glm-5.3", sandbox="read-only"),
                  tmp_path / "p.md", "P", 60)
    perm = json.loads(inv.env["OPENCODE_CONFIG_CONTENT"])["permission"]
    assert perm == {"edit": "deny", "write": "deny", "patch": "deny", "bash": "deny"}


def test_agy_sandbox_read_only_maps_to_plan_mode(tmp_path):
    # Used to raise "not expressible" — true when agy had only the boolean --sandbox
    # (terminal restrictions, no write protection), stale since --mode (accept-edits,
    # plan) landed. Plan mode rejects the write RPC at the permission layer, so it is
    # the same kind of lock claude gets from --permission-mode plan.
    a = H.AgyAdapter.__new__(H.AgyAdapter)
    inv = a.build(AgentSpec(harness="agy", sandbox="read-only"), tmp_path / "p.md", "P", 60)
    assert "--mode" in inv.argv and inv.argv[inv.argv.index("--mode") + 1] == "plan"
    assert "--dangerously-skip-permissions" not in inv.argv
    assert inv.argv[-2] == "--print"          # still last: it eats the next token


def test_agy_without_sandbox_keeps_full_permissions(tmp_path):
    a = H.AgyAdapter.__new__(H.AgyAdapter)
    inv = a.build(AgentSpec(harness="agy"), tmp_path / "p.md", "P", 60)
    assert "--dangerously-skip-permissions" in inv.argv and "--mode" not in inv.argv


def test_sandbox_unknown_value_is_e_harness(tmp_path):
    # the build-time backstop for a value that got past load (e.g. a template
    # that rendered to a typo)
    a = H.ClaudeAdapter.__new__(H.ClaudeAdapter)
    with pytest.raises(EngineCrash) as exc:
        a.build(AgentSpec(harness="claude-code", sandbox="readonly"),
                tmp_path / "p.md", "P", 60)
    assert exc.value.code == "E_HARNESS" and "not one of" in exc.value.message


def test_shell_bodies_use_bash_not_the_login_shell(monkeypatch, tmp_path):
    # A workflow is committed code: it must not change meaning because the operator
    # runs zsh (which does NOT word-split `$var`, so `for x in $list` looped once).
    import medulla.v2.procrun as P
    monkeypatch.setenv("SHELL", "/bin/zsh")
    monkeypatch.delenv("MEDULLA_SHELL", raising=False)
    captured = {}

    def fake_popen(argv, **kw):
        captured["argv"] = argv
        raise RuntimeError("stop here")

    monkeypatch.setattr(P.subprocess, "Popen", fake_popen)
    with pytest.raises(RuntimeError):
        P.run("for x in $list; do echo $x; done", cwd=tmp_path, timeout_s=5)
    assert captured["argv"][0] == "bash" and captured["argv"][1] == "-lc"


def test_medulla_shell_overrides_the_default(monkeypatch, tmp_path):
    import medulla.v2.procrun as P
    monkeypatch.setenv("SHELL", "/bin/zsh")
    monkeypatch.setenv("MEDULLA_SHELL", "/bin/sh")
    captured = {}

    def fake_popen(argv, **kw):
        captured["argv"] = argv
        raise RuntimeError("stop here")

    monkeypatch.setattr(P.subprocess, "Popen", fake_popen)
    with pytest.raises(RuntimeError):
        P.run("echo hi", cwd=tmp_path, timeout_s=5)
    assert captured["argv"][0] == "/bin/sh"


def test_each_harness_runs_its_own_binary_by_name(tmp_path):
    # There is no per-workflow executable override any more: `harness_bin:` is gone and
    # AgentSpec.bin with it. A machine that routes a CLI through a broker installs the
    # wrapper UNDER THE CLI'S OWN NAME, so the engine keeps calling `codex` and never
    # learns what stands behind it.
    for cls, expected in ((H.CodexAdapter, "codex"), (H.ClaudeAdapter, "claude"),
                          (H.OpenCodeAdapter, "opencode"), (H.AgyAdapter, "agy")):
        a = cls.__new__(cls)
        inv = a.build(AgentSpec(harness=expected), tmp_path / "p.md", "P", 60)
        assert inv.argv[0] == expected


def test_claude_reports_a_plan_limit_instead_of_a_bare_rc(tmp_path):
    # Live capture: the weekly limit arrives as an ordinary error result plus a
    # rate_limit_event, so the engine only saw "body died: rc=1" and spent a second
    # attempt on something that cannot clear until the reset.
    a = H.ClaudeAdapter.__new__(H.ClaudeAdapter)
    stream = "\n".join([
        json.dumps({"type": "rate_limit_event", "rate_limit_info": {
            "status": "rejected", "rateLimitType": "seven_day"}}),
        json.dumps({"type": "result", "is_error": True, "api_error_status": 429,
                    "result": "You've hit your weekly limit · resets Aug 21, 3pm (UTC)"}),
    ])
    msg = a.retry_pointless(stream)
    assert msg and "plan limit" in msg and "seven-day" in msg
    # and it must NOT be escalated to a run-killing error: other panelists are fine
    assert a.fatal_error(stream) is None


def test_healthy_output_is_not_mistaken_for_a_limit(tmp_path):
    a = H.ClaudeAdapter.__new__(H.ClaudeAdapter)
    stream = json.dumps({"type": "result", "is_error": False, "result": "done"})
    assert a.retry_pointless(stream) is None
