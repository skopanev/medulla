"""Image eviction: a tag is the sha of its Dockerfile, so builds accumulate.

Three images of one pack, 17 GB, none running. Content addressing without eviction is
a leak — and reaping is scoped to one repository, because the daemon is shared.
"""
import importlib.util
import os
from pathlib import Path

import pytest

# ── image eviction ───────────────────────────────────────────────────────────

def test_prune_keeps_only_the_build_that_just_ran(monkeypatch, capsys):
    """A tag here is the sha of its Dockerfile, so every edit mints a new image and
    the old one stays forever — three project-manager images, 17 GB, none running."""
    import sys as _sys
    from pathlib import Path as _P
    _sys.path.insert(0, str(_P(__file__).resolve().parent.parent / "scripts"))
    from dockerlib import image as im

    listed = ("aaa\tmedulla-pm:new\n"
              "bbb\tmedulla-pm:mid\n"
              "ccc\tmedulla-pm:old\n"
              "ddd\tmedulla-pm:older\n")
    removed = []

    class _R:
        def __init__(self, stdout="", rc=0):
            self.stdout, self.returncode, self.stderr = stdout, rc, ""

    def fake_run(cmd, **kw):
        if cmd[:2] == ["docker", "images"]:
            assert cmd[2] == "medulla-pm"        # scoped to ONE repository
            return _R(listed)
        if cmd[:2] == ["docker", "rmi"]:
            assert "-f" not in cmd               # a running image must be able to refuse
            removed.append(cmd[2])
            return _R()
        raise AssertionError(cmd)

    monkeypatch.setattr(im.subprocess, "run", fake_run)
    im.prune_old_images("medulla-pm:new")
    assert removed == ["bbb", "ccc", "ddd"]      # only the build that just ran survives


def test_prune_counts_builds_not_tags(monkeypatch):
    """One image can carry several tags; keeping 2 must mean two BUILDS."""
    import sys as _sys
    from pathlib import Path as _P
    _sys.path.insert(0, str(_P(__file__).resolve().parent.parent / "scripts"))
    from dockerlib import image as im

    listed = ("aaa\tmedulla-pm:new\naaa\tmedulla-pm:latest\n"
              "bbb\tmedulla-pm:mid\nccc\tmedulla-pm:old\n")
    removed = []

    class _R:
        def __init__(self, stdout="", rc=0):
            self.stdout, self.returncode, self.stderr = stdout, rc, ""

    def fake_run(cmd, **kw):
        if cmd[:2] == ["docker", "images"]:
            return _R(listed)
        removed.append(cmd[2])
        return _R()

    monkeypatch.setattr(im.subprocess, "run", fake_run)
    im.prune_old_images("medulla-pm:new")
    assert removed == ["bbb", "ccc"]             # aaa survives under BOTH its tags


def test_a_failed_build_evicts_nothing(monkeypatch, tmp_path):
    """The previous image is exactly what you fall back to when a build breaks."""
    import sys as _sys
    from pathlib import Path as _P
    _sys.path.insert(0, str(_P(__file__).resolve().parent.parent / "scripts"))
    from dockerlib import image as im

    called = []
    monkeypatch.setattr(im, "prune_old_images", lambda *a, **k: called.append(a))

    class _Proc:
        returncode = 1
        pid = 1
        def wait(self): return 1
    monkeypatch.setattr(im.subprocess, "Popen", lambda *a, **k: _Proc())
    df = tmp_path / "Dockerfile"
    df.write_text("FROM alpine\n")
    im.ensure_image("x:1", True, None, {}, dockerfile=df)
    assert called == []
