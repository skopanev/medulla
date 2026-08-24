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


def test_env_file_unlinked_on_every_exit_path(dockerpy, tmp_path):
    # panel FIX-FIRST #3: the old 5s timer thread died with the process on
    # Ctrl-C / early return and leaked merged tokens in $TMPDIR forever.
    # cleanup is now finally + atexit — must be idempotent (both fire).
    f = tmp_path / "medulla-env-x"
    f.write_text("TOKEN=secret\n", encoding="utf-8")
    dockerpy.dockerenv.env_file_for_run = str(f)   # it lives in dockerlib.env now
    dockerpy._unlink_env_file()
    assert not f.exists() and dockerpy.dockerenv.env_file_for_run is None
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


def test_runs_under_is_relative_not_a_container_path(dockerpy, tmp_path, monkeypatch):
    # --print-run-dir hands the run dir to a caller on the HOST while the run happened
    # inside the container, so an absolute /workspace/... path is useless to them —
    # caught live when a panel printed a run dir that could not be listed.
    home = tmp_path / "home"
    shared = home / ".medulla" / "workflows" / "spar"
    shared.mkdir(parents=True)
    (shared / "workflow.yaml").write_text("version: '2'\n", encoding="utf-8")
    cwd = tmp_path / "repo"; cwd.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.setenv("PWD", str(cwd))
    monkeypatch.chdir(cwd)

    resolved = dockerpy._config_yaml(Path(".medulla/workflows/spar"))
    assert resolved == shared / "workflow.yaml"       # cascade found the shared copy
    # the value handed to the engine must be repo-relative
    dest = Path(".medulla/workflows/spar")
    assert not str(dest).startswith("/")


def test_agy_keychain_is_not_triggered_by_the_word_agy(dockerpy, tmp_path):
    # It used to be `"agy" in yaml.read_text()`: the word in a PROMPT or a comment sent
    # the runner into the macOS Keychain for credentials the run never needed.
    w = tmp_path / "workflow.yaml"
    w.write_text("""version: "2"
start: a
# we used to run agy here, not any more
nodes:
  a:
    agent: {harness: claude-code, model: sonnet}
    prompt: "compare this with agy output"
    on_signal: {ok: __exit_ok__}
""", encoding="utf-8")
    assert dockerpy.workflow_uses_agy(str(w)) is False


def test_agy_is_detected_where_it_is_actually_declared(dockerpy, tmp_path):
    for decl in ('agent: {harness: agy}', 'agent: agy'):
        w = tmp_path / "workflow.yaml"
        w.write_text(f"""version: "2"
start: a
nodes:
  a:
    {decl}
    prompt: "hi"
    on_signal: {{ok: __exit_ok__}}
""", encoding="utf-8")
        assert dockerpy.workflow_uses_agy(str(w)) is True, decl


def test_agy_detected_when_a_pool_input_carries_the_harness(dockerpy, tmp_path):
    # spar declares harnesses as pool DATA, not on the agent node
    w = tmp_path / "workflow.yaml"
    w.write_text("""version: "2"
start: p
nodes:
  p:
    inputs:
      - {slug: gemini, harness: agy, model: "Gemini 3.1 Pro (High)"}
    agent: {harness: "{{input.harness}}"}
    prompt: "hi"
    on_signal: {__done__: __exit_ok__}
""", encoding="utf-8")
    assert dockerpy.workflow_uses_agy(str(w)) is True


def test_unreadable_workflow_keeps_the_permissive_answer(dockerpy, tmp_path):
    assert dockerpy.workflow_uses_agy(str(tmp_path / "nope.yaml")) is True


def test_overlay_carries_a_symlinked_package_directory_whole(dockerpy, tmp_path, monkeypatch):
    """A wrapper on PATH is useless if the package it imports cannot follow it in.

    Real directories under the overlay are still walked file-by-file (never mounted
    as a directory over /usr/local/bin, which would hide the image's own CLIs), but a
    symlink to a directory travels whole to its own nested path — it covers nothing
    but itself.
    """
    home = tmp_path / "home"
    (home / ".medulla" / "container" / "bin").mkdir(parents=True)
    lib = tmp_path / "real-lib" / "pkg"
    lib.mkdir(parents=True)
    (lib / "__init__.py").write_text("", encoding="utf-8")
    wrapper = home / ".medulla" / "container" / "bin" / "wrap"
    wrapper.write_text("#!/bin/sh\n", encoding="utf-8")
    nested = home / ".medulla" / "container" / "home" / ".local" / "lib"
    nested.mkdir(parents=True)
    (nested / "pkg").symlink_to(lib)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))

    vols = dockerpy.build_volumes(tmp_path / "claude-home", mount_agy=False)
    mounts = " ".join(vols)
    assert f"{wrapper}:/usr/local/bin/wrap:ro" in mounts          # the file, as before
    assert f"{lib}:{dockerpy.dockermounts.CONTAINER_HOME}/.local/lib/pkg:ro" in mounts   # the package


def test_sighup_does_not_kill_a_backgrounded_run(dockerpy):
    """`medulla ... &` exists so a 10-20 minute panel outlives the shell that started
    it. SIGHUP arrives when that shell goes away, and unhandled it killed the run
    mid-flight — a panel that had already written 105KB of one answer vanished without
    a manifest, which read as medulla dying on its own. Deliberate interruption is
    untouched: SIGINT and SIGTERM still stop the run and the container with it.
    """
    import inspect
    import signal

    src = inspect.getsource(dockerpy.main)
    assert "SIGHUP" in src and "SIG_IGN" in src
    assert signal.getsignal(signal.SIGINT) is not signal.SIG_IGN   # still interruptible


# ── image eviction ───────────────────────────────────────────────────────────

def test_prune_keeps_the_newest_builds_of_that_repository_only(monkeypatch, capsys):
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
    assert removed == ["ccc", "ddd"]             # the two newest survive


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
    assert removed == ["ccc"]


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
