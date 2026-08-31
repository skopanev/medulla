"""The container-reuse races a panel found, all reachable with today's code.

Waiting for readiness only in the create branch, a check-then-create on one unique
name, and an identity that ignored what the container actually IS.
"""
import subprocess
import sys


def test_every_caller_waits_for_readiness_not_just_the_creator(monkeypatch):
    """Three panelists found this independently: waiting only in the create branch
    left whoever arrives SECOND free to exec while the entrypoint was still upgrading
    medulla — the exact race the idle-start rewrite existed to remove."""
    from dockerlib import session_run

    waited, execs = [], []

    class _R:
        returncode = 0
        stdout = ""
        stderr = ""

    class _Proc:
        returncode = 0
        def wait(self): return 0

    monkeypatch.setattr(session_run.keep, "sweep_stale", lambda *a, **k: 0)
    monkeypatch.setattr(session_run.keep, "is_running", lambda name: True)   # already up
    monkeypatch.setattr(session_run.keep, "remove", lambda name: None)
    monkeypatch.setattr(session_run, "_wait_ready", lambda name, **k: waited.append(name) or True)
    monkeypatch.setattr(session_run.subprocess, "run", lambda cmd, **kw: _R())
    monkeypatch.setattr(session_run.subprocess, "Popen",
                        lambda cmd, **kw: execs.append(cmd) or _Proc())

    session_run._run_kept("img:1", ["-v", "/a:/a"], ["-w", "wf"], None, None)
    assert waited, "a caller joining a running container must wait for the marker too"


def test_losing_the_creation_race_joins_instead_of_failing(monkeypatch):
    """Two first callers can both see the container absent. The loser gets "name
    already in use" — which is not an error, it is someone else winning."""
    from dockerlib import session_run

    state = {"running": False}

    class _Fail:
        returncode = 125
        stdout = ""
        stderr = "docker: Error response from daemon: name already in use"

    class _Proc:
        returncode = 0
        def wait(self): return 0

    def is_running(name):
        # absent on the check, present by the time `docker run` has failed
        was = state["running"]
        state["running"] = True
        return was

    monkeypatch.setattr(session_run.keep, "sweep_stale", lambda *a, **k: 0)
    monkeypatch.setattr(session_run.keep, "is_running", is_running)
    monkeypatch.setattr(session_run.keep, "remove", lambda name: None)
    monkeypatch.setattr(session_run, "_wait_ready", lambda name, **k: True)
    monkeypatch.setattr(session_run.subprocess, "run", lambda cmd, **kw: _Fail())
    monkeypatch.setattr(session_run.subprocess, "Popen", lambda cmd, **kw: _Proc())

    assert session_run._run_kept("img:1", [], ["-w", "wf"], None, None) == 0


def test_a_different_image_mount_or_shadow_gets_its_own_container():
    """Keying on the pipeline alone let a later nested workflow land in a container
    built from another image, or writable when it asked for --cwd-ro."""
    from dockerlib import keep

    a = keep.spec_digest("img:1", ["-v", "/repo:/workspace:ro"])
    b = keep.spec_digest("img:2", ["-v", "/repo:/workspace:ro"])
    c = keep.spec_digest("img:1", ["-v", "/repo:/workspace"])       # writable
    d = keep.spec_digest("img:1", ["-v", "/repo:/workspace:ro"], ["secrets"])
    assert len({a, b, c, d}) == 4
    assert keep.container_name("pipe1", a) != keep.container_name("pipe1", c)


def test_idle_container_is_created_without_forwarded_environment(monkeypatch):
    """The long-lived sleep process has no use for caller credentials."""
    from dockerlib import env as dockerenv
    from dockerlib import session_run

    monkeypatch.setattr(dockerenv, "env_values_for_run", {
        "ANTHROPIC_API_KEY": "host-sentinel",
        "PROJECT_TIER": "dotenv-sentinel",
    })
    monkeypatch.setenv("ANTHROPIC_API_KEY", "host-sentinel")
    starts = []

    class _R:
        returncode = 0
        stdout = ""
        stderr = ""

    class _Proc:
        returncode = 0

        def wait(self):
            return 0

    def run(cmd, **kwargs):
        starts.append((cmd, kwargs))
        return _R()

    monkeypatch.setattr(session_run.keep, "sweep_stale", lambda *a, **k: 0)
    monkeypatch.setattr(session_run.keep, "is_running", lambda name: False)
    monkeypatch.setattr(session_run.keep, "remove", lambda name: None)
    monkeypatch.setattr(session_run, "_wait_ready", lambda name, **k: True)
    monkeypatch.setattr(session_run.subprocess, "run", run)
    monkeypatch.setattr(session_run.subprocess, "Popen", lambda cmd, **kw: _Proc())

    assert session_run._run_kept("img:1", [], ["-w", "wf"], None, None) == 0

    argv, _kwargs = starts[0]
    assert "ANTHROPIC_API_KEY" not in argv
    assert "PROJECT_TIER" not in argv
    assert all("sentinel" not in token for token in argv)


def test_a_joining_exec_carries_values_only_in_child_environment(monkeypatch):
    """A joining exec inherited the FIRST caller's keys and .env tiers, so a later
    nested run could authenticate as the earlier one — or find nothing at all, if the
    first call had no key and this one does."""
    from dockerlib import env as dockerenv
    from dockerlib import session_run

    monkeypatch.setattr(dockerenv, "env_values_for_run", {
        "ANTHROPIC_API_KEY": "this-calls-key",
        "PROJECT_TIER": "from-dotenv",
    })
    monkeypatch.setenv("ANTHROPIC_API_KEY", "this-calls-key")

    execs = []

    class _Proc:
        returncode = 0
        def wait(self): return 0

    monkeypatch.setattr(session_run.keep, "sweep_stale", lambda *a, **k: 0)
    monkeypatch.setattr(session_run.keep, "is_running", lambda name: True)
    monkeypatch.setattr(session_run.keep, "remove", lambda name: None)
    monkeypatch.setattr(session_run, "_wait_ready", lambda name, **k: True)
    monkeypatch.setattr(session_run.subprocess, "Popen",
                        lambda cmd, **kw: execs.append((cmd, kw)) or _Proc())

    session_run._run_kept("img:1", [], ["-w", "wf"], None, None)

    argv, kwargs = execs[0]
    assert all("this-calls-key" not in token for token in argv)
    assert all("from-dotenv" not in token for token in argv)
    assert kwargs["env"]["ANTHROPIC_API_KEY"] == "this-calls-key"
    assert kwargs["env"]["PROJECT_TIER"] == "from-dotenv"
    assert argv[argv.index("ANTHROPIC_API_KEY") - 1] == "-e"
    assert argv[argv.index("PROJECT_TIER") - 1] == "-e"


def test_agent_child_removes_another_harness_environment(monkeypatch, tmp_path):
    from conftest import fake_script, write_workflow
    from medulla.v2.engine import run_workflow
    from medulla.v2.secret_policy import POLICY_ENV, encoded_policy

    agent = fake_script(tmp_path, "agent.sh", '''
test "$OWN_TOKEN" = "own" || exit 7
test -z "${RIVAL_TOKEN:-}" || exit 8
echo "<signal:ok>isolated</signal:ok>"
''')
    workflow, work = write_workflow(tmp_path, f'''
version: "2"
start: one
nodes:
  one:
    agent: {{harness: fake, model: "{agent}"}}
    prompt: test
    on_signal: {{ok: __exit_ok__}}
''')
    policy = {"version": 1, "bundles": [], "all_env": ["OWN_TOKEN", "RIVAL_TOKEN"],
              "harnesses": {"fake": {"env": ["OWN_TOKEN"], "bundles": []}}}
    monkeypatch.setenv(POLICY_ENV, encoded_policy(policy))
    monkeypatch.setenv("OWN_TOKEN", "own")
    monkeypatch.setenv("RIVAL_TOKEN", "rival")

    assert run_workflow(workflow, workdir=work) == 0


def test_google_application_credentials_mount_keeps_forwarded_path(dockerpy, tmp_path):
    credentials = tmp_path / "google.json"
    credentials.write_text("{}", encoding="utf-8")
    values = {"GOOGLE_APPLICATION_CREDENTIALS": str(credentials)}

    mounts = dockerpy.build_volumes(
        tmp_path / "no-claude",
        credential_bundles={"google-application-credentials"},
        credential_env=values,
    )

    assert f"{credentials}:{credentials}:ro" in mounts
    assert values["GOOGLE_APPLICATION_CREDENTIALS"] == str(credentials.resolve())
