"""Body and workflow deadlines cover the process, not runner setup."""
import builtins
import sys
import time
from types import SimpleNamespace

from medulla.v2 import engine_body, engine_inputs, procrun


def test_log_open_does_not_consume_body_runtime(tmp_path, monkeypatch):
    real_open = builtins.open
    log_path = tmp_path / "attempt.log"

    def delayed_open(path, *args, **kwargs):
        if path == log_path:
            time.sleep(0.2)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", delayed_open)
    result = procrun.run(
        [sys.executable, "-c", "import time; time.sleep(0.15)"],
        tmp_path, timeout_s=0.25, log_path=log_path,
    )
    assert result.rc == 0 and not result.timed_out


def test_absolute_deadline_caps_body_wait(tmp_path):
    started = time.monotonic()
    result = procrun.run(
        [sys.executable, "-c", "import time; time.sleep(10)"],
        tmp_path, timeout_s=1, hard_deadline=started + 0.2,
    )
    assert result.timed_out and time.monotonic() - started < 0.5


def test_capture_start_at_deadline_is_a_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(procrun.OutputCapture, "start", lambda *_args: False)
    result = procrun.run(
        [sys.executable, "-c", "import time; time.sleep(10)"],
        tmp_path, timeout_s=0,
    )
    assert result.timed_out and result.rc == procrun.TIMEOUT_RC


def test_success_gets_bounded_final_drain(tmp_path, monkeypatch):
    real_finish = procrun.OutputCapture.finish
    budgets = []

    def track_finish(capture, deadline):
        budgets.append(deadline - time.monotonic())
        return real_finish(capture, deadline)

    monkeypatch.setattr(procrun.OutputCapture, "finish", track_finish)
    result = procrun.run(
        [sys.executable, "-c", "print('tail')"], tmp_path, timeout_s=3,
    )
    assert result.rc == 0 and result.stdout == "tail\n"
    assert 0.05 <= budgets[0] <= 0.11


def test_pre_and_input_sources_receive_workflow_deadline(tmp_path, monkeypatch):
    seen = []

    def fake_run(*_args, **kwargs):
        seen.append(kwargs.get("hard_deadline"))
        return procrun.RunResult(0, False, "one\n", "")

    monkeypatch.setattr(engine_body, "proc_run", fake_run)
    monkeypatch.setattr(engine_inputs, "proc_run", fake_run)
    owner = SimpleNamespace(
        deadline=123.0, workdir=tmp_path, p=SimpleNamespace(timeout=10),
        _clamp=lambda value: value, _base_env=lambda: {},
        _render_or_crash=lambda value, *_args: value,
    )
    node = SimpleNamespace(
        name="n", pre="true", action=SimpleNamespace(kind="shell"),
        pool=SimpleNamespace(inputs=SimpleNamespace(
            data=None, shell="printf one", shell_timeout=1,
        )),
    )
    guard = engine_body.BodyMixin._run_pre_hook(
        owner, node, tmp_path, lambda value, *_args: value,
        lambda _vars: None, lambda: {}, set(),
    )[0]
    inputs = engine_inputs.InputsMixin._materialize_inputs(owner, node, tmp_path)
    assert guard is None and inputs == ["one"]
    assert seen == [123.0, 123.0]
