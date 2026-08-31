"""What the container gets in its environment.

The .env tiers are collected whole, then docker.secrets must explicitly grant every
key that crosses the container boundary.
"""
import importlib.util
import os
from pathlib import Path

import pytest


@pytest.fixture
def dockerpy():
    spec = importlib.util.spec_from_file_location(
        "dockerpy", Path(__file__).resolve().parent.parent / "scripts" / "docker.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_tier_merge_nearest_wins_all_tiers_whole(dockerpy, tmp_path, monkeypatch):
    # Collection stays whole; the later policy gate rejects undeclared keys explicitly.
    home = tmp_path / "home"
    (home / ".medulla").mkdir(parents=True)
    (home / ".medulla" / ".env").write_text(
        "CLAUDE_CODE_OAUTH_TOKEN=global\nSLACK_TOKEN=global-slack\n", encoding="utf-8")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    project = tmp_path / "proj"
    pdir = project / ".medulla" / "workflows" / "pipe"
    pdir.mkdir(parents=True)
    (project / ".medulla" / ".env").write_text("OPENAI_API_KEY=proj\n", encoding="utf-8")
    (pdir / ".env").write_text("CLAUDE_CODE_OAUTH_TOKEN=workflow-wins\n", encoding="utf-8")
    monkeypatch.chdir(project)          # project tier = the repo you launch from

    env = dockerpy._collect_dotenv(str(pdir))
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "workflow-wins"  # nearest wins
    assert env["OPENAI_API_KEY"] == "proj"                    # flows down
    assert env["SLACK_TOKEN"] == "global-slack"               # ALL tiers whole (user's zone)


def test_claude_token_home_is_fallback_only(dockerpy, tmp_path, monkeypatch):
    home = tmp_path / "home"
    token_dir = home / ".claude"
    token_dir.mkdir(parents=True)
    (token_dir / "token-home").write_text("profile-token\n", encoding="utf-8")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

    env = {}
    dockerpy._add_claude_token_fallback(env)
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "profile-token"

    env = {"CLAUDE_CODE_OAUTH_TOKEN": "dotenv-token"}
    dockerpy._add_claude_token_fallback(env)
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "dotenv-token"

    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "shell-token")
    env = {}
    dockerpy._add_claude_token_fallback(env)
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env


def test_claude_token_home_rejects_multiple_lines(dockerpy, tmp_path, monkeypatch):
    home = tmp_path / "home"
    token_dir = home / ".claude"
    token_dir.mkdir(parents=True)
    (token_dir / "token-home").write_text("first\nsecond\n", encoding="utf-8")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

    with pytest.raises(SystemExit, match="must be one line"):
        dockerpy._add_claude_token_fallback({})


def test_shadow_mounts_tmpfs_and_no_block_is_byte_identical(dockerpy, tmp_path):
    wdir = tmp_path / "wf"
    wdir.mkdir()
    (wdir / "workflow.yaml").write_text(
        'version: "2"\ndocker:\n  shadow: [secrets, sub/dir/]\n', encoding="utf-8")
    assert dockerpy.read_shadow_paths(str(wdir)) == ["secrets", "sub/dir"]

    base = dockerpy.build_run_command("img", [], ["-w", "x"], "c1")
    dockerpy.dockerpaths.shadow_paths_for_run = ["secrets"]
    shadowed = dockerpy.build_run_command("img", [], ["-w", "x"], "c1")
    i = shadowed.index("--tmpfs")
    assert shadowed[i + 1] == "/workspace/secrets"

    dockerpy.dockerpaths.shadow_paths_for_run = []           # acceptance: no block ->
    assert dockerpy.build_run_command("img", [], ["-w", "x"], "c1") == base


def test_shadow_escape_fails_fast_and_reads_legacy_name(dockerpy, tmp_path):
    wdir = tmp_path / "wf"
    wdir.mkdir()
    # legacy filename on purpose: the block must be readable there too
    (wdir / "pipeline.yaml").write_text(
        'version: "2"\ndocker: {shadow: ["../up"]}\n', encoding="utf-8")
    with pytest.raises(SystemExit, match="escapes"):
        dockerpy.read_shadow_paths(str(wdir))


def test_docker_run_carries_values_only_in_child_environment(dockerpy, monkeypatch):
    from dockerlib import process
    from medulla.v2.secret_policy import POLICY_ENV

    monkeypatch.setattr(dockerpy.dockerenv, "env_values_for_run", {
        "ANTHROPIC_API_KEY": "host-sentinel",
        "PROJECT_TOKEN": "dotenv-sentinel",
        POLICY_ENV: '{"all_env":["ANTHROPIC_API_KEY","PROJECT_TOKEN"]}',
    })
    captured = {}

    class _Proc:
        pid = 123
        returncode = 0

        def wait(self):
            return 0

    def popen(cmd, **kwargs):
        captured.update(argv=cmd, kwargs=kwargs)
        return _Proc()

    monkeypatch.setattr(process.subprocess, "Popen", popen)
    monkeypatch.setattr(process.signal, "signal", lambda *args: None)

    assert dockerpy.run_docker("img", [], ["-w", "wf"]) == 0
    argv, child_env = captured["argv"], captured["kwargs"]["env"]
    assert all("sentinel" not in token for token in argv)
    assert "--env-file" not in argv
    assert child_env["ANTHROPIC_API_KEY"] == "host-sentinel"
    assert child_env["PROJECT_TOKEN"] == "dotenv-sentinel"
    assert argv[argv.index("ANTHROPIC_API_KEY") - 1] == "-e"
    assert argv[argv.index("PROJECT_TOKEN") - 1] == "-e"


def test_literal_pool_harnesses_build_a_finite_policy(tmp_path):
    from medulla.v2.secret_policy import resolve_policy
    workflow = tmp_path / "workflow.yaml"
    workflow.write_text('''version: "2"
nodes:
  panel:
    inputs:
      - {harness: codex}
      - {harness: claude-code}
    agent: {harness: "{{input.harness}}"}
''', encoding="utf-8")
    policy = resolve_policy(str(workflow))
    assert set(policy["harnesses"]) == {"claude-code", "codex"}
    assert "OPENAI_API_KEY" in policy["all_env"]
    assert "GEMINI_API_KEY" not in policy["all_env"]


def test_dynamic_and_unknown_harnesses_fail_before_docker(tmp_path):
    from medulla.v2.secret_policy import SecretPolicyError, resolve_policy
    workflow = tmp_path / "workflow.yaml"
    workflow.write_text('''version: "2"
nodes:
  panel:
    inputs: {shell: "printf codex"}
    agent: {harness: "{{input.harness}}"}
''', encoding="utf-8")
    with pytest.raises(SecretPolicyError, match="finite list"):
        resolve_policy(str(workflow))
    workflow.write_text('''version: "2"
nodes: {one: {agent: {harness: private-cli}}}
''', encoding="utf-8")
    with pytest.raises(SecretPolicyError, match="unknown harness"):
        resolve_policy(str(workflow))


def test_undeclared_dotenv_fails_and_agents_remove_rival_env(tmp_path):
    from medulla.v2.secret_policy import (
        SecretPolicyError,
        encoded_policy,
        env_keys_to_remove,
        resolve_policy,
        select_env_values,
    )
    workflow = tmp_path / "workflow.yaml"
    workflow.write_text('''version: "2"
docker:
  secrets:
    harnesses: [claude-code, codex]
    grants: {claude-code: {env: [SLACK_TOKEN]}}
nodes: {}
''', encoding="utf-8")
    policy = resolve_policy(str(workflow))
    with pytest.raises(SecretPolicyError, match="UNDECLARED_TOKEN"):
        select_env_values(policy, {"UNDECLARED_TOKEN": "secret"}, {})
    assert select_env_values(policy, {"SLACK_TOKEN": "ok"}, {})["SLACK_TOKEN"] == "ok"
    assert "OPENAI_API_KEY" in env_keys_to_remove("claude-code", encoded_policy(policy))
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env_keys_to_remove(
        "claude-code", encoded_policy(policy))


def test_credential_mounts_follow_selected_bundles(dockerpy, tmp_path, monkeypatch):
    home = tmp_path / "home"
    for path in (home / ".claude", home / ".codex", home / ".gemini"):
        path.mkdir(parents=True)
    auth = home / ".local" / "share" / "opencode" / "auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    mounts = " ".join(dockerpy.build_volumes(
        home / ".claude", credential_bundles={"codex"},
    ))
    assert "/mnt/codex" in mounts
    assert "/mnt/claude" not in mounts
    assert "/mnt/gemini" not in mounts
    assert "/mnt/opencode-auth.json" not in mounts


def test_workflow_may_be_a_yaml_file_not_only_a_dir(dockerpy, tmp_path):
    # `-w dir/other.yaml` is valid on the CLI side (v2/cli.py::_resolve_workflow_yaml),
    # so it must be valid under --docker too — otherwise the same command works bare
    # and dies containerised, hunting for "other.yaml/workflow.yaml".
    brain = tmp_path / "brain"
    brain.mkdir()
    yaml_file = brain / "resolve.yaml"
    yaml_file.write_text("version: '2'\nstart: x\nnodes:\n  x:\n    shell: echo hi\n",
                         encoding="utf-8")
    assert dockerpy._config_yaml(yaml_file) == yaml_file


def test_relative_dockerfile_resolves_against_the_dir_even_in_file_mode(dockerpy, tmp_path):
    # The regression: workflow_dir was Path(workflow) verbatim, so a relative
    # vars.DOCKERFILE became "brain/resolve.yaml/Dockerfile.custom" — unopenable.
    brain = tmp_path / "brain"
    brain.mkdir()
    (brain / "Dockerfile.custom").write_text("FROM scratch\n", encoding="utf-8")
    yaml_file = brain / "resolve.yaml"
    yaml_file.write_text("version: '2'\nvars:\n  DOCKERFILE: Dockerfile.custom\n"
                         "start: x\nnodes:\n  x:\n    shell: echo hi\n", encoding="utf-8")
    df = dockerpy.resolve_dockerfile(str(yaml_file), {})
    assert df == brain / "Dockerfile.custom" and df.is_file()


def test_image_tag_drops_the_yaml_extension_but_keeps_dir_names_whole(dockerpy, tmp_path):
    brain = tmp_path / "brain"
    brain.mkdir()
    df = brain / "Dockerfile.custom"
    df.write_text("FROM scratch\n", encoding="utf-8")
    yaml_file = brain / "resolve.yaml"
    yaml_file.write_text("version: '2'\n", encoding="utf-8")
    assert dockerpy.image_tag_for(str(yaml_file), df).startswith("medulla-resolve:")
    dotted = tmp_path / "my.workflows"       # a DIRECTORY with a dot keeps its full name
    dotted.mkdir()
    assert dockerpy.image_tag_for(str(dotted), df).startswith("medulla-my.workflows:")
