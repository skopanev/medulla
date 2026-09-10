"""What the container gets in its environment.

The .env tiers are collected whole and cross into the container whole — shell bodies
are what they are for. An agent body inherits only what its harness was granted.
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
    # Collection stays whole: the split happens per body, not at the container edge.
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


def test_business_keys_reach_the_container_but_never_an_agent(tmp_path):
    """A business key is not an undeclared credential.

    It belongs to the shell nodes the .env was written for, and the agent must not
    read it. The old gate refused the run instead, and the only way past that error
    was to grant a business key to a harness — making the model's environment worse to
    make the message go away.
    """
    from medulla.v2.secret_policy import (
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
    # A real .env is hundreds of names nobody enumerates. None of these appear
    # anywhere in medulla, and that is the point: the rule reads the grant, never
    # a list of keys it was taught to distrust.
    business = {f"PROJECT_KEY_{n}": f"v{n}" for n in range(500)}
    dotenv = {**business, "SLACK_TOKEN": "ok"}

    # The container gets all of it: shell bodies are what the .env is for.
    values = select_env_values(policy, dotenv, {})
    assert all(values[name] == value for name, value in business.items())
    assert values["SLACK_TOKEN"] == "ok"

    encoded = encoded_policy(policy)
    remove = set(env_keys_to_remove("claude-code", encoded, present=dotenv))
    assert business.keys() <= remove, "not one business key may reach a model"
    assert "OPENAI_API_KEY" in remove, "nor a rival harness credential"
    assert "SLACK_TOKEN" not in remove, "an explicit grant survives"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in remove, "nor its own credential"


def test_a_bare_run_strips_too(tmp_path):
    """No policy travels outside Docker, but the engine still knows what it merged.

    Host system vars are never candidates, so PATH and HOME survive untouched.
    """
    from medulla.v2.secret_policy import env_keys_to_remove
    remove = env_keys_to_remove("codex", "", present={"PROJECT_KEY_1", "PATH"})
    assert "PROJECT_KEY_1" in remove
    assert "ANTHROPIC_API_KEY" in remove, "rival credential goes even with no policy"
    assert "OPENAI_API_KEY" not in remove, "codex still authenticates"
    assert env_keys_to_remove("codex", "", present=()) == sorted(
        {"ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "ZHIPU_API_KEY",
         "GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_CLOUD_PROJECT",
         "GOOGLE_APPLICATION_CREDENTIALS", "VERTEX_LOCATION"})


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


def test_a_var_with_a_literal_default_is_not_dynamic(tmp_path):
    """One HARNESS var with a literal default is the common shape, not a puzzle.

    Refusing it made 118 of 213 live definitions unrunnable and asked each to
    declare a list that only restated the file. A value the launcher can already
    see — a default in `vars:`, or a `--var` just typed — is known before
    `docker run`.
    """
    from medulla.v2.secret_policy import resolve_policy
    workflow = tmp_path / "workflow.yaml"
    workflow.write_text('''version: "2"
vars: {HARNESS: codex, MODEL: gpt-5.6}
nodes:
  one: {agent: {harness: "{{var:HARNESS}}", model: "{{var:MODEL}}"}}
''', encoding="utf-8")
    assert set(resolve_policy(str(workflow))["harnesses"]) == {"codex"}

    # The launcher's --var wins, and the policy follows it rather than the default.
    policy = resolve_policy(str(workflow), {"HARNESS": "claude-code"})
    assert set(policy["harnesses"]) == {"claude-code"}
    assert "OPENAI_API_KEY" not in policy["all_env"], "the default's keys do not linger"


def test_a_harness_only_a_run_can_know_still_fails_closed(tmp_path):
    """A var built from another var, or one no one declared, stays dynamic."""
    from medulla.v2.secret_policy import SecretPolicyError, resolve_policy
    workflow = tmp_path / "workflow.yaml"
    workflow.write_text('''version: "2"
vars: {HARNESS: "{{var:PICKED}}"}
nodes:
  one: {agent: {harness: "{{var:HARNESS}}"}}
''', encoding="utf-8")
    with pytest.raises(SecretPolicyError, match="cannot be resolved"):
        resolve_policy(str(workflow))

    workflow.write_text('''version: "2"
nodes:
  one: {agent: {harness: "{{var:NEVER_DECLARED}}"}}
''', encoding="utf-8")
    with pytest.raises(SecretPolicyError, match="cannot be resolved"):
        resolve_policy(str(workflow))
