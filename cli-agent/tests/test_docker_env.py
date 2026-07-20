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
