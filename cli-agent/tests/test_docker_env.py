"""docker.py env forwarding: tier order, passlist, nearest-wins."""
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


def test_tier_merge_nearest_wins_and_passlist(dockerpy, tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".medulla").mkdir(parents=True)
    (home / ".medulla" / ".env").write_text(
        "ANTHROPIC_API_KEY=global\nSLACK_TOKEN=leak-me-not\n", encoding="utf-8")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    project = tmp_path / "proj"
    pdir = project / ".medulla" / "pipelines" / "pipe"
    pdir.mkdir(parents=True)
    (project / ".medulla" / ".env").write_text(
        "OPENAI_API_KEY=proj\nTELEGRAM_TOKEN=leak-me-not\n", encoding="utf-8")
    (pdir / ".env").write_text(
        "ANTHROPIC_API_KEY=pipeline-wins\nMY_PIPE_SECRET=ok-pipeline-scoped\n",
        encoding="utf-8")

    env = dockerpy._collect_dotenv(str(pdir))
    assert env["ANTHROPIC_API_KEY"] == "pipeline-wins"   # nearest wins, explicitly
    assert env["OPENAI_API_KEY"] == "proj"               # harness key flows down
    assert env["MY_PIPE_SECRET"] == "ok-pipeline-scoped" # pipeline tier: whole
    assert "SLACK_TOKEN" not in env                      # outer tiers: passlist only
    assert "TELEGRAM_TOKEN" not in env
