"""What each adapter actually executes: argv, preflight, and the binary check.

The CLIs disagree about everything — flags, subcommands, where the prompt rides — so\nthe argv each one builds is checked directly rather than through a run.
"""
import json
import os
import stat
from pathlib import Path

import pytest
from medulla.v2 import harness as H
from medulla.v2.errors import EngineCrash
from medulla.v2.model import AgentSpec


@pytest.fixture(autouse=True)
def clean_registry():
    H.reset_registry()
    yield
    H.reset_registry()


def make_bin(dir_: Path, name: str, body: str) -> Path:
    p = dir_ / name
    p.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return p


@pytest.fixture
def on_path(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    return bindir



# ── argv construction (pure) ────────────────────────────────────────────────

def test_claude_argv_and_env(tmp_path):
    a = H.ClaudeAdapter.__new__(H.ClaudeAdapter)   # skip binary check
    inv = a.build(AgentSpec(harness="claude-code", model="sonnet"),
                  tmp_path / "prompt.md", "PROMPT", 600)
    assert inv.argv[0] == "claude"
    assert "--append-system-prompt-file" in inv.argv
    assert inv.argv[-2:] == ["-p", "Execute."]
    assert "--model" in inv.argv and "sonnet" in inv.argv
    assert inv.env["API_TIMEOUT_MS"] == str((600 + 300) * 1000)
    assert "ANTHROPIC_API_KEY" in inv.env_remove
    assert inv.stdin is None


def test_codex_argv_stdin_and_effort(tmp_path):
    a = H.CodexAdapter.__new__(H.CodexAdapter)
    inv = a.build(AgentSpec(harness="codex", model="gpt-5.5", effort="xhigh"),
                  tmp_path / "prompt.md", "BIG PROMPT", 900)
    assert inv.stdin == "BIG PROMPT"               # stdin delivery, not argv/@file
    assert "-c" in inv.argv and 'model="gpt-5.5"' in inv.argv
    assert "model_reasoning_effort=xhigh" in inv.argv
    assert f"stream_idle_timeout_ms={(900 + 300) * 1000}" in inv.argv
    assert "--dangerously-bypass-approvals-and-sandbox" in inv.argv
    assert "--full-auto" not in inv.argv           # deprecated AND sandboxed


def test_codex_user_args_come_last(tmp_path):
    a = H.CodexAdapter.__new__(H.CodexAdapter)
    inv = a.build(AgentSpec(harness="codex", args=["-c", "model_reasoning_effort=low"]),
                  tmp_path / "p.md", "P", 60)
    # codex layering: last -c wins — author overrides must follow the defaults
    assert inv.argv[-2:] == ["-c", "model_reasoning_effort=low"]


def test_agy_print_is_last_flag(tmp_path):
    a = H.AgyAdapter.__new__(H.AgyAdapter)
    inv = a.build(AgentSpec(harness="agy", model="gemini-3.1-pro",
                            args=["--add-dir", "/x"]),
                  tmp_path / "p.md", "THE PROMPT", 120)
    # THE trap: --print consumes the next token as the prompt; it must be last
    assert inv.argv[-2] == "--print" and inv.argv[-1] == "THE PROMPT"
    assert inv.argv.index("--add-dir") < inv.argv.index("--print")
    assert "Gemini 3.1 Pro (High)" in inv.argv     # alias resolved
    assert "--print-timeout" in inv.argv and "420s" in inv.argv


def test_agy_unknown_model_passes_verbatim(tmp_path):
    a = H.AgyAdapter.__new__(H.AgyAdapter)
    inv = a.build(AgentSpec(harness="agy", model="Claude Opus 4.6 (Thinking)"),
                  tmp_path / "p.md", "P", 60)
    assert "Claude Opus 4.6 (Thinking)" in inv.argv


def test_opencode_prompt_rides_stdin(tmp_path):
    # The prompt must NOT be a positional argv string: Linux caps one argv string
    # at MAX_ARG_STRLEN (131072B), so a big prompt used to die with E2BIG.
    a = H.OpenCodeAdapter.__new__(H.OpenCodeAdapter)
    inv = a.build(AgentSpec(harness="opencode", model="zai/glm-5.3"),
                  tmp_path / "p.md", "PROMPT TEXT", 60)
    assert inv.argv[:2] == ["opencode", "run"]
    assert "--agent" in inv.argv                    # order is free, presence is not
    assert inv.stdin == "PROMPT TEXT"
    assert "PROMPT TEXT" not in inv.argv
    assert "-m" in inv.argv and "zai/glm-5.3" in inv.argv


def test_opencode_huge_prompt_stays_off_argv(tmp_path):
    # 150KB — over MAX_ARG_STRLEN. Must build cleanly (no tripwire, no E2BIG).
    a = H.OpenCodeAdapter.__new__(H.OpenCodeAdapter)
    big = "x" * 150_000
    inv = a.build(AgentSpec(harness="opencode", model="zai/glm-5.3"),
                  tmp_path / "p.md", big, 60)
    assert inv.stdin == big
    assert max(len(s.encode()) for s in inv.argv) < 1_000


def test_invoke_tripwire_rejects_oversized_argv():
    # Regression guard for the whole adapter family, not just opencode.
    with pytest.raises(EngineCrash) as e:
        H.Invoke(argv=["cli", "x" * 150_000])
    assert "E2BIG" in str(e.value)


# ── prepare() ────────────────────────────────────────────────────────────────

def test_opencode_config_rides_in_env(tmp_path):
    # ported from main@217f751: no on-disk opencode.json — the config layers
    # via OPENCODE_CONFIG_CONTENT, per-invocation (heterogeneous efforts work)
    a = H.OpenCodeAdapter.__new__(H.OpenCodeAdapter)
    spec = AgentSpec(harness="opencode", model="zai/glm-5.3", effort="high")
    inv = a.build(spec, tmp_path / "p.md", "P", 600)
    cfg = json.loads(inv.env["OPENCODE_CONFIG_CONTENT"])
    assert cfg["permission"] == "allow"
    assert cfg["provider"]["zai"]["options"]["timeout"] == (600 + 300) * 1000
    assert cfg["provider"]["zai"]["models"]["glm-5.3"]["options"]["reasoningEffort"] == "high"
    assert not (tmp_path / "opencode.json").exists()


def test_agy_untrusted_workspace_is_e_harness(tmp_path, monkeypatch):
    monkeypatch.delenv("MEDULLA_DOCKER", raising=False)
    monkeypatch.setattr(H, "_agy_trusted", lambda wd: False)
    a = H.AgyAdapter.__new__(H.AgyAdapter)
    with pytest.raises(EngineCrash) as exc:
        a.prepare(AgentSpec(harness="agy"), tmp_path)
    assert exc.value.code == "E_HARNESS" and "trustedWorkspaces" in exc.value.message


def test_agy_trust_skipped_in_docker(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDULLA_DOCKER", "1")
    monkeypatch.setattr(H, "_agy_trusted", lambda wd: False)
    H.AgyAdapter.__new__(H.AgyAdapter).prepare(AgentSpec(harness="agy"), tmp_path)


# ── resolve: binary check = the E_HARNESS razor ─────────────────────────────

def test_missing_binary_is_e_harness(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)   # adapters import their own
    with pytest.raises(EngineCrash) as exc:
        H.resolve(AgentSpec(harness="codex"))
    assert exc.value.code == "E_HARNESS" and "on PATH" in exc.value.message


def test_unknown_harness_is_e_harness():
    with pytest.raises(EngineCrash) as exc:
        H.resolve(AgentSpec(harness="nonsense"))
    assert exc.value.code == "E_HARNESS"


