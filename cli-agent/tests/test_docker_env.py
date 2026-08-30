"""What the container gets in its environment.

The .env tiers forward whole while the host shell is filtered by an
allowlist — a shell carries hundreds of unrelated variables and forwarding it leaks.
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

    monkeypatch.setattr(dockerpy.dockerenv, "env_values_for_run", {
        "PROJECT_TOKEN": "dotenv-sentinel",
    })
    monkeypatch.setenv("ANTHROPIC_API_KEY", "host-sentinel")
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

