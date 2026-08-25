import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

from medulla.v2.contract import load_workflow
from medulla.v2.errors import EngineCrash

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # cli-agent/

os.environ["MEDULLA_RETRY_DELAY_S"] = "0"   # retry backoff off in tests
os.environ["MEDULLA_STREAM"] = "0"          # operator streaming off in tests


# ── the three helpers every integration test needs ───────────────────────────
# They lived in test_hooks_agents.py and four other files imported them FROM a test
# module, which made that file a dependency of tests it has nothing to do with — and
# made it impossible to split without breaking them. Fixtures belong in conftest.

def write_workflow(tmp_path, text, name="pipe"):
    """A workflow directory plus a separate working directory, the way a run has them.

    Returns (yaml_path, workdir). The two are separate on purpose: a body writing into
    its cwd must not land in the definition, and several tests check exactly that.
    """
    pdir = tmp_path / name
    pdir.mkdir(exist_ok=True)
    (pdir / "workflow.yaml").write_text(text, encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    return pdir / "workflow.yaml", work


def read_run(pdir, index=0):
    """(run_dir, outcome, journal) for a run under `pdir`. index picks which run."""
    import json
    runs = sorted((pdir / "runs").iterdir())
    run = runs[index]
    outcome_path = run / "outcome.json"
    outcome = json.loads(outcome_path.read_text()) if outcome_path.is_file() else {}
    jp = run / "journal.jsonl"
    journal = [json.loads(l) for l in jp.read_text().splitlines()] if jp.exists() else []
    return run, outcome, journal


def fake_script(tmp_path, name, body):
    """A script the `fake` harness runs as an agent body."""
    p = tmp_path / name
    p.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
    return str(p)


@pytest.fixture
def workflow(tmp_path):
    """write_workflow bound to this test's tmp_path: `yaml, work = workflow(text)`."""
    return lambda text, name="pipe": write_workflow(tmp_path, text, name)


@pytest.fixture
def script(tmp_path):
    """fake_script bound to this test's tmp_path."""
    return lambda name, body: fake_script(tmp_path, name, body)

def setup_workflow(tmp_path, text):
    pdir = tmp_path / "pipe"
    pdir.mkdir()
    (pdir / "workflow.yaml").write_text(text, encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    return pdir / "workflow.yaml", work


@pytest.fixture
def dockerpy():
    spec = importlib.util.spec_from_file_location(
        "dockerpy", Path(__file__).resolve().parent.parent / "scripts" / "docker.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A machine: a HOME with a shared workflow root, and a project to launch from.

    Both resolvers read Path.home(); the project is the cwd, because that is what
    --docker mounts as /workspace and what a relative -w is spelled against.
    """
    home = tmp_path / "home"
    (home / ".medulla" / "workflows").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    project = tmp_path / "proj"
    (project / ".medulla" / "workflows").mkdir(parents=True)
    monkeypatch.chdir(project)
    monkeypatch.setenv("PWD", str(project))   # docker.py reads $PWD, not just getcwd()

    class World:
        def __init__(self):
            self.home = home
            self.project = project

        def shared(self, name, *, yaml_name="workflow.yaml", body=WORKFLOW_BODY, env=None):
            d = home / ".medulla" / "workflows" / name
            d.mkdir(parents=True, exist_ok=True)
            (d / yaml_name).write_text(body, encoding="utf-8")
            if env:
                (d / ".env").write_text(env, encoding="utf-8")
            return d

        def local(self, name, *, yaml_name="workflow.yaml", body=WORKFLOW_BODY, env=None):
            d = project / ".medulla" / "workflows" / name
            d.mkdir(parents=True, exist_ok=True)
            (d / yaml_name).write_text(body, encoding="utf-8")
            if env:
                (d / ".env").write_text(env, encoding="utf-8")
            return d

        def anchor(self, name):
            """The directory 12 repos actually have: no yaml, just a home for runs/."""
            d = project / ".medulla" / "workflows" / name
            d.mkdir(parents=True, exist_ok=True)
            return d

    return World()


def engine_yaml(w):
    from medulla.v2.cli import _resolve_workflow_yaml
    return _resolve_workflow_yaml(Path(w))


def resolve_or_raise(w):
    return engine_yaml(w)


def same_file(a, b):
    """Identity of the FILE, not the spelling of the path: both resolvers hand back
    the path as it was given (deliberately — an absolutised one breaks
    --print-run-dir across the container boundary), so a relative and an absolute
    spelling of one file are the same answer here."""
    return Path(a).resolve() == Path(b).resolve()


def engine_env(w):
    from medulla.v2.engine import load_dotenv
    return load_dotenv(engine_yaml(w).parent)


WORKFLOW_BODY = ("version: '2'\nstart: a\nnodes:\n  a:\n"
                 "    shell: 'true'\n    on_signal: {ok: __exit_ok__}\n")


POOL_RESUME = """
version: "2"
start: p
timeout: {timeout}
nodes:
  p:
    inputs: {{shell: "echo source >> {work}/source-calls; printf 'a\\nb\\nc\\nd\\n'"}}
    shell: |
      echo run >> "{work}/body-$MEDULLA_INPUT"
      if [ "$MEDULLA_INPUT" = d ] && [ ! -f "{work}/second-pass" ]; then sleep 30; fi
    timeout: 300
    on_signal: {{__done__: __exit_ok__}}
"""


def runs_of(pdir):
    return sorted((pdir / "runs").iterdir())


def read_outcome(run):
    return json.loads((run / "outcome.json").read_text())

def work_dir(tmp_path):
    return str(tmp_path / "work")


DECISION_RESUME = """
version: "2"
start: a
timeout: 2
nodes:
  a:
    shell: 'echo a >> {work}/a-runs; echo "<signal:go>from-a</signal:go>"'
    on_signal: {{go: b}}
  b:
    shell: |
      echo b >> {work}/b-runs
      if [ ! -f {work}/fast ]; then sleep 30; fi
      [ "$MEDULLA_LAST_MESSAGE" = "from-a" ] && echo "<signal:ok>k</signal:ok>"
    timeout: 300
    on_signal: {{ok: __exit_ok__}}
"""

def _is_outside_workspace(dockerpy, w):
    """Ask docker.py itself — a copy of the rule here would only confirm the copy."""
    return dockerpy.definition_is_outside_workspace(dockerpy._config_yaml(Path(w)))

def read_manifest(run, step_name):
    mp = run / "steps" / step_name / "manifest.jsonl"
    return [json.loads(l) for l in mp.read_text().splitlines()] if mp.exists() else []

def write(tmp_path, text):
    p = tmp_path / "workflow.yaml"
    p.write_text(text, encoding="utf-8")
    return p

def load_err(tmp_path, text) -> str:
    with pytest.raises(EngineCrash) as exc:
        load_workflow(write(tmp_path, text))
    assert exc.value.code == "E_VALIDATION"
    return exc.value.message

MINIMAL = """
version: "2"
start: a
nodes:
  a:
    shell: "true"
    on_signal: {ok: __exit_ok__}
"""
