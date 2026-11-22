"""The workflow DEFINITION reaching the container: where it comes from, how it rides.

Carved out of test_docker_mounts.py under the project's 250-line rule ($MAX_LOC), and
it follows the code: the mount itself moved from docker.py into
dockerlib/definition.py. A definition that does not arrive is the failure that reads
as a broken tool — the file is plainly there on the host and missing inside.
"""
from pathlib import Path


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


def test_the_workflow_is_mounted_as_a_DIRECTORY(tmp_path, monkeypatch):
    """A bind mount of a FILE is bound to its inode, and `medulla refresh` publishes
    definitions by replacing them atomically — the correct way to write a file someone
    may be reading. But the new name points at a new inode, and every container already
    mounting the old one loses the file outright. Measured directly: `cat` inside a
    container goes from the file's contents to "No such file or directory" the instant
    the host replaces it; with the directory mounted instead, it sees the new content.

    That was the FileNotFoundError on medulla's own workflow.yaml that killed rounds
    launched during a refresh — presenting as a healthy start, since the launcher
    prints a run directory and a pid before anything fails.
    """
    import sys as _s
    from pathlib import Path as _P
    _s.path.insert(0, str(_P(__file__).resolve().parent.parent / "scripts"))
    from dockerlib import definition

    shared = tmp_path / "workflows" / "spar"
    (shared / "prompts").mkdir(parents=True)
    (shared / "workflow.yaml").write_text("version: '2'\nstart: a\nnodes: {}\n")
    (shared / "prompts" / "p.md").write_text("x\n")

    # Patch the SOURCE modules, not this caller's names: every consumer then sees the
    # same answer, and the test does not pin which module the mount is built in. The
    # previous version patched docker.py and called a function that never existed
    # there, so it asserted on hand-built flags and tested nothing of the real path.
    monkeypatch.setattr("dockerlib.paths.definition_is_outside_workspace",
                        lambda p: True)
    monkeypatch.setattr("dockerlib.image._config_yaml",
                        lambda p: shared / "workflow.yaml")
    monkeypatch.setattr("dockerlib.paths.runs_under_for", lambda p: _P("runs"))

    volumes: list[str] = []
    args, runs_under = definition.mount_shared_definition(
        volumes, ["-w", str(shared)], str(shared))
    assert runs_under == "runs"
    assert args == ["-w", "/mnt/medulla-workflows/spar/workflow.yaml"]

    # The command builder is what actually ships the flags; assert on its output.
    from dockerlib.process import build_run_command
    cmd = build_run_command("img", volumes, args, "medulla-x")
    joined = " ".join(cmd)
    assert f"{shared}:/mnt/medulla-workflows/spar:ro" in joined
    assert "workflow.yaml:ro" not in joined, "still mounting the file — inode-bound"


def test_the_source_ships_a_directory_mount_for_the_definition(tmp_path):
    """Read the real source rather than a stub: a refactor is exactly what would
    silently reintroduce the file mount.

    The guard followed the code. The mount moved out of docker.py into
    dockerlib/definition.py when docker.py was carved under the 250-line rule, and a
    guard left reading the old file would have passed on an empty match forever."""
    import sys as _s
    from pathlib import Path as _P
    src = (_P(__file__).resolve().parent.parent / "scripts" / "dockerlib"
           / "definition.py").read_text()
    assert 'f"{resolved.parent}:{mnt}:ro"' in src
    assert 'f"{resolved}:{mnt}/workflow.yaml:ro"' not in src, "the file mount is back"
