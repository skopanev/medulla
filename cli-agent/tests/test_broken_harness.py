"""A wrapper that cannot start is not a model failure, and a retry repeats it exactly.

Reported from two consecutive five-model rounds: `cx` — a credential-refreshing
wrapper the container installs — died with ModuleNotFoundError before reviewing a
line, twice per run. The manifest said `rc=1`, so it read as a provider problem and an
hour went into quota dashboards.
"""
import json

from conftest import fake_script, read_run
from conftest import write_workflow as setup
from medulla.v2.engine import run_workflow
from medulla.v2.harness import resolve as resolve_harness
from medulla.v2.model import AgentSpec

TRACEBACK = '''Traceback (most recent call last):
  File "/usr/local/bin/cx", line 13, in <module>
    from hltm.cli import main
ModuleNotFoundError: No module named 'hltm'
'''


def test_the_signature_is_recognised_on_stdout_and_stderr():
    a = resolve_harness(AgentSpec(harness="codex"))
    assert "failed to start" in (a.broken_launch(TRACEBACK) or "")
    assert "failed to start" in (a.broken_launch("", TRACEBACK) or "")
    assert "hltm" in a.broken_launch(TRACEBACK)


def test_a_missing_binary_counts_too():
    a = resolve_harness(AgentSpec(harness="codex"))
    assert a.broken_launch("", "cx: command not found") is not None
    assert a.broken_launch("", "bash: /usr/local/bin/cx: No such file or directory") is not None


def test_an_ordinary_model_failure_is_not_a_broken_launch():
    a = resolve_harness(AgentSpec(harness="codex"))
    assert a.broken_launch("the model declined to answer", "") is None
    assert a.broken_launch("", "rate limit exceeded") is None


def test_a_broken_launch_costs_one_attempt_not_two(tmp_path):
    """The point of the whole thing: the second attempt would be identical."""
    agent = fake_script(tmp_path, "broken.sh", """
echo "run" >> attempts.log
echo "Traceback (most recent call last):" >&2
echo "ModuleNotFoundError: No module named 'hltm'" >&2
exit 1
""")
    yaml, work = setup(tmp_path, f"""
version: "2"
start: a
nodes:
  a:
    agent: {{harness: fake, model: "{agent}"}}
    prompt: "go"
    max_attempts: 3
    on_signal: {{done: __exit_ok__, __failed__: __exit_fail__}}
""")
    run_workflow(yaml, workdir=work)
    assert (work / "attempts.log").read_text().count("run") == 1


def test_the_manifest_says_what_broke(tmp_path):
    """A pool row that reads `rc=1` sends the reader to the wrong dashboard."""
    agent = fake_script(tmp_path, "broken.sh",
                        'echo "ModuleNotFoundError: No module named hltm" >&2\nexit 1\n')
    yaml, work = setup(tmp_path, f"""
version: "2"
start: p
nodes:
  p:
    inputs: [one]
    agent: {{harness: fake, model: "{agent}"}}
    prompt: "{{{{input}}}}"
    on_signal: {{__done__: __exit_ok__, __failed__: __exit_fail__}}
""")
    run_workflow(yaml, workdir=work)
    run = sorted((yaml.parent / "runs").iterdir())[0]
    row = json.loads((run / "steps" / "001-p" / "manifest.jsonl").read_text().splitlines()[0])
    assert "failed to start" in row["message"]
    assert "hltm" in row["message"]
