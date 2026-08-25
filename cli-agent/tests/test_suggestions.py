"""Fixes for the field report in medulla-suggestions.md — one test per point.

Each of these was found by running a real pack against the engine for a week, and
each was silent: the run did not fail, it did the wrong thing quietly.
"""
import os
import subprocess
import sys
from pathlib import Path

from medulla.v2.engine import run_workflow
from conftest import _is_outside_workspace, read_run, write_workflow as setup

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


# ── point 4: cwd is the truth, PWD is only a spelling of it ──────────────────

def test_a_stale_pwd_does_not_choose_the_mount(tmp_path, monkeypatch):
    """subprocess.run(cwd=X) leaves PWD pointing at the CALLER, and it is non-empty —
    so `PWD or getcwd()` never fell back. The container got the caller's directory and
    the run died with `workflow not found`, saying nothing about mounts."""
    sys.path.insert(0, str(SCRIPTS))
    from dockerlib.paths import workspace_cwd

    real = tmp_path / "real"
    real.mkdir()
    monkeypatch.chdir(real)
    monkeypatch.setenv("PWD", str(tmp_path / "somewhere-else"))
    assert workspace_cwd() == Path(os.getcwd())


def test_pwd_is_kept_when_it_names_the_same_directory(tmp_path, monkeypatch):
    """It is preferred when it agrees: it preserves the symlink form the user typed,
    and that spelling has to stay valid on the host side of a -v argument."""
    sys.path.insert(0, str(SCRIPTS))
    from dockerlib.paths import workspace_cwd

    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    monkeypatch.chdir(real)
    monkeypatch.setenv("PWD", str(link))
    assert workspace_cwd() == link


def test_workflow_not_found_names_what_is_visible(tmp_path, monkeypatch):
    """Inside a container this is a mount problem far more often than a typo."""
    from medulla.v2.contract import load_workflow
    from medulla.v2.errors import EngineCrash

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MEDULLA_DOCKER", "1")
    (tmp_path / ".medulla" / "workflows" / "other").mkdir(parents=True)
    try:
        load_workflow(tmp_path / "missing.yaml")
    except EngineCrash as exc:
        assert "cwd inside the container" in exc.message
        assert "other" in exc.message          # names what IS here
    else:
        raise AssertionError("a missing workflow must crash")


# ── point 5: budgets that cannot be honoured ────────────────────────────────

def test_a_node_budget_over_the_run_budget_warns(tmp_path, capsys):
    """A sweep node inherited 3600 while the workflow it launched declared 21600: the
    agent planned for six hours and was killed at one, with nothing in the log."""
    from medulla.v2.contract import load_workflow

    yaml, _work = setup(tmp_path, """
version: "2"
timeout: 3600
start: a
nodes:
  a:
    shell: 'true'
    timeout: 21600
    on_signal: {ok: __exit_ok__}
""")
    load_workflow(yaml)
    err = capsys.readouterr().err
    assert "node 'a' asks for 21600s" in err and "3600s" in err


def test_an_unlimited_workflow_warns_about_nothing(tmp_path, capsys):
    from medulla.v2.contract import load_workflow

    yaml, _work = setup(tmp_path, """
version: "2"
timeout: 0
start: a
nodes:
  a:
    shell: 'true'
    timeout: 21600
    on_signal: {ok: __exit_ok__}
""")
    load_workflow(yaml)
    assert "asks for" not in capsys.readouterr().err


# ── point 7: a node that printed nothing ────────────────────────────────────

def test_the_default_route_explains_itself(tmp_path):
    """"did its job, wrote its file, printed nothing, and failed" is correct and
    startling — and was only discoverable by reading the journal."""
    yaml, work = setup(tmp_path, """
version: "2"
start: a
nodes:
  a:
    shell: 'echo "wrote a file, said nothing"'
    on_signal: {ok: __exit_ok__}
""")
    run_workflow(yaml, workdir=work)
    _run, _out, journal = read_run(yaml.parent)
    assert "took __default__" in journal[0]["message"]
    assert "route it, or print a signal" in journal[0]["message"]


# ── point 3: a worktree INSIDE the project, not as the project ──────────────

def test_a_nested_worktrees_git_dir_is_mounted(tmp_path, monkeypatch):
    """`git worktree add` writes an absolute HOST path into the worktree's .git file.
    The engine already mounted the common .git when CWD ITSELF was the worktree — but
    a pack that hands an agent an isolated copy usually creates it INSIDE the project,
    and then every git command inside the container answered
    `fatal: not a git repository: (null)`."""
    sys.path.insert(0, str(SCRIPTS))
    from dockerlib.mounts import build_volumes

    app = tmp_path / "app"
    app.mkdir()
    subprocess.run(["git", "init", "-q", "."], cwd=app, check=True)
    (app / "a.txt").write_text("hi")
    subprocess.run(["git", "add", "-A"], cwd=app, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-qm", "init"], cwd=app, check=True)
    subprocess.run(["git", "worktree", "add", "-q", "-b", "wt", str(tmp_path / "wt")],
                   cwd=app, check=True)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PWD", str(tmp_path))
    vols = build_volumes(Path("/nonexistent"), mount_agy=False)

    common = str((app / ".git").resolve())
    mounted = [v for v in vols if v.startswith(common + ":")]
    assert mounted, f"the common .git was not mounted; got {vols}"
    # at its OWN path, which is what makes the worktree's absolute pointer resolve
    assert mounted[0].split(":")[1] == common


def test_every_workflow_directory_reaches_the_container(tmp_path, monkeypatch):
    """A shared definition is mounted file by file, and the list was hardcoded to
    ("prompts",) — so scripts/ never arrived and the synthesize node's collector did
    not exist inside. The panel wrote no verdict.md and still reported success."""
    sys.path.insert(0, str(SCRIPTS))
    import docker as dockerpy  # noqa: F401  (the runner script)

    shared = tmp_path / "home" / ".medulla" / "workflows" / "spar"
    for sub in ("prompts", "scripts", "templates"):
        (shared / sub).mkdir(parents=True)
    (shared / "workflow.yaml").write_text("version: '2'\nstart: n\nnodes: {}\n")
    (shared / "runs").mkdir()

    from dockerlib.image import _config_yaml
    monkeypatch.setattr("dockerlib.paths.definition_is_outside_workspace", lambda _p: True)

    resolved = _config_yaml(shared)
    mounted = []
    for src in sorted(d for d in resolved.parent.iterdir() if d.is_dir()):
        if src.name in ("runs", "__pycache__"):
            continue
        mounted.append(src.name)
    assert mounted == ["prompts", "scripts", "templates"]
    assert "runs" not in mounted                    # history, not definition
