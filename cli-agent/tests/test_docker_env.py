"""docker.py env forwarding: tier order, nearest-wins, secrets-file lifecycle."""
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
    dockerpy.shadow_paths_for_run = ["secrets"]
    shadowed = dockerpy.build_run_command("img", [], ["-w", "x"], "c1")
    i = shadowed.index("--tmpfs")
    assert shadowed[i + 1] == "/workspace/secrets"

    dockerpy.shadow_paths_for_run = []           # acceptance: no block ->
    assert dockerpy.build_run_command("img", [], ["-w", "x"], "c1") == base


def test_shadow_escape_fails_fast_and_reads_legacy_name(dockerpy, tmp_path):
    wdir = tmp_path / "wf"
    wdir.mkdir()
    # legacy filename on purpose: the block must be readable there too
    (wdir / "pipeline.yaml").write_text(
        'version: "2"\ndocker: {shadow: ["../up"]}\n', encoding="utf-8")
    with pytest.raises(SystemExit, match="escapes"):
        dockerpy.read_shadow_paths(str(wdir))


def test_env_file_unlinked_on_every_exit_path(dockerpy, tmp_path):
    # panel FIX-FIRST #3: the old 5s timer thread died with the process on
    # Ctrl-C / early return and leaked merged tokens in $TMPDIR forever.
    # cleanup is now finally + atexit — must be idempotent (both fire).
    f = tmp_path / "medulla-env-x"
    f.write_text("TOKEN=secret\n", encoding="utf-8")
    dockerpy.env_file_for_run = str(f)
    dockerpy._unlink_env_file()
    assert not f.exists() and dockerpy.env_file_for_run is None
    dockerpy._unlink_env_file()                               # second call is a no-op


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


def test_symlinked_workflow_target_is_mounted_at_the_same_path(dockerpy, tmp_path, monkeypatch):
    # A shared workflow lives outside the repo (~/.medulla/workflows/<name>) and only cwd
    # is mounted, so inside the container the link dangles: "workflow not found". Its
    # target must be mounted read-only at the SAME /workspace path the link occupies.
    shared = tmp_path / "shared" / "spar"
    shared.mkdir(parents=True)
    real = shared / "workflow.yaml"
    real.write_text("version: '2'\n", encoding="utf-8")

    cwd = tmp_path / "repo"
    (cwd / ".medulla" / "workflows" / "spar").mkdir(parents=True)
    (cwd / ".medulla" / "workflows" / "spar" / "workflow.yaml").symlink_to(real)

    monkeypatch.setenv("PWD", str(cwd))
    monkeypatch.chdir(cwd)
    vols = dockerpy.build_volumes(tmp_path / "no-claude", mount_agy=False)
    spec = f"{real}:/workspace/.medulla/workflows/spar/workflow.yaml:ro"
    assert spec in vols
    # the directory itself is NOT mounted: runs/ must stay repo-local
    assert not any(v.startswith(f"{shared}:") for v in vols)


def test_workflow_symlink_pointing_inside_the_workspace_is_not_remounted(dockerpy, tmp_path, monkeypatch):
    # Already inside /workspace — mounting it again would be noise, and a local real file
    # (the override case) must not produce a mount at all.
    cwd = tmp_path / "repo"
    (cwd / ".medulla" / "workflows" / "a").mkdir(parents=True)
    real = cwd / "shared-here.yaml"
    real.write_text("version: '2'\n", encoding="utf-8")
    (cwd / ".medulla" / "workflows" / "a" / "workflow.yaml").symlink_to(real)
    (cwd / ".medulla" / "workflows" / "b").mkdir(parents=True)
    (cwd / ".medulla" / "workflows" / "b" / "workflow.yaml").write_text("version: '2'\n", encoding="utf-8")

    monkeypatch.setenv("PWD", str(cwd))
    monkeypatch.chdir(cwd)
    vols = dockerpy.build_volumes(tmp_path / "no-claude", mount_agy=False)
    assert not any("/workspace/.medulla/workflows/" in v for v in vols)


def test_broken_workflow_symlink_does_not_break_the_run(dockerpy, tmp_path, monkeypatch):
    cwd = tmp_path / "repo"
    (cwd / ".medulla" / "workflows" / "x").mkdir(parents=True)
    (cwd / ".medulla" / "workflows" / "x" / "workflow.yaml").symlink_to(tmp_path / "gone.yaml")
    monkeypatch.setenv("PWD", str(cwd))
    monkeypatch.chdir(cwd)
    vols = dockerpy.build_volumes(tmp_path / "no-claude", mount_agy=False)   # must not raise
    assert not any("gone.yaml" in v for v in vols)
