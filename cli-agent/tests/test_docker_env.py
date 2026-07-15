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
    pdir = project / ".medulla" / "pipelines" / "pipe"
    pdir.mkdir(parents=True)
    (project / ".medulla" / ".env").write_text("OPENAI_API_KEY=proj\n", encoding="utf-8")
    (pdir / ".env").write_text("CLAUDE_CODE_OAUTH_TOKEN=pipeline-wins\n", encoding="utf-8")

    env = dockerpy._collect_dotenv(str(pdir))
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "pipeline-wins"  # nearest wins
    assert env["OPENAI_API_KEY"] == "proj"                    # flows down
    assert env["SLACK_TOKEN"] == "global-slack"               # ALL tiers whole (user's zone)


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
