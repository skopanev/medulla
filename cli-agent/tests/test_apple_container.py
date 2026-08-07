"""Apple container command generation without starting VMs."""
import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def apple(monkeypatch):
    monkeypatch.delenv("MEDULLA_APPLE_CPUS", raising=False)
    monkeypatch.delenv("MEDULLA_APPLE_MEMORY", raising=False)
    source = Path(__file__).resolve().parent.parent / "scripts" / "apple_container.py"
    spec = importlib.util.spec_from_file_location("apple_container", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.shared.dockerenv.env_file_for_run = None
    module.shared.dockerpaths.shadow_paths_for_run = []
    return module


def test_build_run_command_matches_medulla_mount_and_sandbox_contract(
        apple, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr(apple.shared, "interactive_stdio", lambda: False)
    monkeypatch.setenv("OPENAI_API_KEY", "token")
    apple.shared.dockerenv.env_file_for_run = "/tmp/merged.env"
    apple.shared.dockerpaths.shadow_paths_for_run = ["secrets"]

    cmd = apple.build_run_command(
        "medulla:test", ["-v", "/host:/workspace"], ["-w", "flow"], "medulla-a")

    assert cmd[:10] == [
        "container", "run", "--init", "--rm", "--name", "medulla-a",
        "--cpus", "4", "--memory", "4g"]
    assert ["-e", "OPENAI_API_KEY=token"] == cmd[10:12]
    assert cmd[cmd.index("--env-file") + 1] == "/tmp/merged.env"
    assert "/host:/workspace" in cmd
    assert cmd[cmd.index("--tmpfs") + 1] == "/workspace/secrets"
    assert "MEDULLA_DOCKER=1" in cmd
    assert cmd[-3:] == ["medulla", "-w", "flow"]


def test_resource_limits_are_host_overridable(apple, monkeypatch):
    monkeypatch.setenv("MEDULLA_APPLE_CPUS", "6")
    monkeypatch.setenv("MEDULLA_APPLE_MEMORY", "6g")
    assert apple.resource_args() == ["--cpus", "6", "--memory", "6g"]


def test_runs_folder_probe_overrides_image_entrypoint(apple, tmp_path, monkeypatch):
    calls = []

    class Result:
        returncode = 0
        stderr = ""

    monkeypatch.setattr(
        apple.subprocess, "run",
        lambda argv, **kwargs: (calls.append(argv), Result())[1])

    apple.assert_runs_folder_reaches_container(tmp_path, "medulla:test")

    cmd = calls[0]
    assert cmd[cmd.index("--entrypoint"):cmd.index("--entrypoint") + 3] == [
        "--entrypoint", "sh", "medulla:test"]
    assert not list(tmp_path.glob(".medulla-mount-probe-*"))


def test_ensure_image_uses_apple_image_inspect(apple, monkeypatch):
    calls = []

    class Result:
        returncode = 0

    monkeypatch.setattr(
        apple.subprocess, "run", lambda argv, **kwargs: (calls.append(argv), Result())[1])
    assert apple.ensure_image("medulla:test", False, None, {}) == 0
    assert calls == [["container", "image", "inspect", "medulla:test"]]


def test_missing_ready_image_pulls_with_apple_cli(apple, monkeypatch):
    calls = []

    class Result:
        def __init__(self, rc):
            self.returncode = rc

    def run(argv, **kwargs):
        calls.append(argv)
        return Result(1 if len(calls) == 1 else 0)

    monkeypatch.setattr(apple.subprocess, "run", run)
    assert apple.ensure_image("registry/medulla:test", False, "flow", {},
                              ready_image=True) == 0
    assert calls == [
        ["container", "image", "inspect", "registry/medulla:test"],
        ["container", "image", "pull", "registry/medulla:test"],
    ]


def test_build_failure_returns_builder_exit_code(apple, tmp_path, monkeypatch):
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM scratch\n", encoding="utf-8")
    calls = []

    class Proc:
        returncode = 17

        def wait(self):
            return self.returncode

    monkeypatch.chdir(tmp_path)
    handlers = {}

    def install(signum, handler):
        previous = handlers.get(signum)
        handlers[signum] = handler
        return previous

    def spawn(argv, **kwargs):
        assert apple.signal.SIGINT in handlers
        assert apple.signal.SIGTERM in handlers
        calls.append(argv)
        return Proc()

    monkeypatch.setattr(apple.subprocess, "Popen", spawn)
    monkeypatch.setattr(apple.signal, "signal", install)

    assert apple.ensure_image(
        "medulla:test", True, None, {}, dockerfile=dockerfile) == 17
    assert calls == [[
        "container", "build", "--cpus", "4", "--memory", "4g",
        "--build-arg", f"USER_UID={apple.os.getuid()}",
        "-f", str(dockerfile), "-t", "medulla:test", "--no-cache", str(tmp_path),
    ]]


@pytest.mark.parametrize("signal_name", ["SIGINT", "SIGTERM"])
def test_interrupted_build_signals_only_its_client_group(
        apple, tmp_path, monkeypatch, signal_name):
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM scratch\n", encoding="utf-8")
    handlers = {}
    forwarded = []
    run_calls = []

    class Proc:
        returncode = -2
        pid = 123

        def wait(self):
            return self.returncode

        def poll(self):
            return self.returncode

    proc = Proc()
    signum = getattr(apple.signal, signal_name)

    def install(signum, handler):
        previous = handlers.get(signum)
        handlers[signum] = handler
        return previous

    def spawn(argv, **kwargs):
        handlers[signum](signum, None)
        return proc

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(apple.signal, "signal", install)
    monkeypatch.setattr(apple.subprocess, "Popen", spawn)
    monkeypatch.setattr(
        apple.subprocess, "run", lambda argv, **kwargs: run_calls.append(argv))
    monkeypatch.setattr(
        apple.apple_build, "signal_process_group",
        lambda target, signum: forwarded.append((target, signum)))

    assert apple.ensure_image(
        "medulla:test", True, None, {}, dockerfile=dockerfile) == 130
    assert forwarded == [(proc, signum)]
    assert run_calls == []


def test_signal_process_group_targets_child_pgid(apple, monkeypatch):
    calls = []

    class Proc:
        pid = 123

    monkeypatch.setattr(apple.os, "getpgid", lambda pid: (calls.append(("get", pid)), 456)[1])
    monkeypatch.setattr(
        apple.os, "killpg", lambda pgid, signum: calls.append(("kill", pgid, signum)))

    apple.signal_process_group(Proc(), apple.signal.SIGTERM)
    assert calls == [("get", 123), ("kill", 456, apple.signal.SIGTERM)]


def test_second_build_interrupt_force_kills_only_client_group(
        apple, tmp_path, monkeypatch):
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM scratch\n", encoding="utf-8")
    handlers = {}
    forwarded = []
    run_calls = []
    cleanup = []

    class Proc:
        returncode = -9
        pid = 123

        def wait(self):
            handlers[apple.signal.SIGINT](apple.signal.SIGINT, None)
            handlers[apple.signal.SIGINT](apple.signal.SIGINT, None)

        def poll(self):
            return self.returncode

    proc = Proc()

    def install(signum, handler):
        previous = handlers.get(signum)
        handlers[signum] = handler
        return previous

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(apple.signal, "signal", install)
    monkeypatch.setattr(apple.subprocess, "Popen", lambda argv, **kwargs: proc)
    monkeypatch.setattr(
        apple.subprocess, "run", lambda argv, **kwargs: run_calls.append(argv))
    monkeypatch.setattr(
        apple.apple_build, "signal_process_group",
        lambda target, signum: forwarded.append((target, signum)))
    class Thread:
        def __init__(self, target, args):
            cleanup.append((target, args))

        def start(self):
            pass

        def join(self):
            pass

    monkeypatch.setattr(apple.threading, "Thread", Thread)

    assert apple.ensure_image(
        "medulla:test", True, None, {}, dockerfile=dockerfile) == 130
    assert forwarded == [(proc, apple.signal.SIGINT)]
    assert len(cleanup) == 1
    assert cleanup[0][0] is apple.force_process_after_grace
    assert cleanup[0][1][1].is_set()
    assert run_calls == []


def test_build_force_after_grace_targets_only_live_client_group(apple, monkeypatch):
    events = []

    class Proc:
        def poll(self):
            return None

    class Wake:
        def wait(self, seconds):
            events.append(("wait", seconds))

    proc = Proc()
    monkeypatch.setattr(
        apple.shared,
        "terminate_process_group",
        lambda target, force: events.append(("terminate", target, force)),
    )

    apple.force_process_after_grace(proc, Wake(), 2)
    assert events == [
        ("wait", 2),
        ("terminate", proc, True),
    ]


def test_build_interrupt_before_spawn_skips_process_creation(
        apple, tmp_path, monkeypatch):
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM scratch\n", encoding="utf-8")
    handlers = {}

    def install(signum, handler):
        previous = handlers.get(signum)
        handlers[signum] = handler
        if signum == apple.signal.SIGTERM and callable(handlers[apple.signal.SIGINT]):
            handlers[apple.signal.SIGINT](apple.signal.SIGINT, None)
        return previous

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(apple.signal, "signal", install)
    monkeypatch.setattr(
        apple.subprocess,
        "Popen",
        lambda argv, **kwargs: pytest.fail("build process must not start"),
    )

    assert apple.ensure_image(
        "medulla:test", True, None, {}, dockerfile=dockerfile) == 130


def test_kill_container_can_forward_sigint(apple, monkeypatch):
    calls = []

    class Proc:
        pass

    monkeypatch.setattr(
        apple.subprocess, "Popen", lambda argv, **kwargs: (calls.append(argv), Proc())[1])
    apple.kill_container("medulla-a", "INT")
    assert calls == [["container", "kill", "--signal", "INT", "medulla-a"]]


def test_delete_container_force_cleans_interrupted_vm(apple, monkeypatch):
    calls = []

    class Result:
        def __init__(self, returncode):
            self.returncode = returncode

    monkeypatch.setattr(
        apple.subprocess, "run", lambda argv, **kwargs: (calls.append((argv, kwargs)), Result(0))[1])
    monkeypatch.setattr(apple, "container_exists", lambda name: False)
    assert apple.delete_container("medulla-a") is True
    assert len(calls) == 1
    assert calls[0][0] == ["container", "delete", "--force", "medulla-a"]
    assert calls[0][1]["timeout"] == 5


def test_container_exists_only_treats_not_found_as_absent(apple, monkeypatch):
    class Result:
        returncode = 1

        def __init__(self, stderr):
            self.stderr = stderr

    monkeypatch.setattr(
        apple.subprocess, "run", lambda argv, **kwargs: Result("service unavailable"))
    assert apple.container_exists("medulla-a") is True

    monkeypatch.setattr(
        apple.subprocess,
        "run",
        lambda argv, **kwargs: Result("Error: container not found: medulla-a"),
    )
    assert apple.container_exists("medulla-a") is False


def test_delete_container_reports_verified_cleanup_failure(apple, monkeypatch, capsys):
    calls = []

    class Result:
        returncode = 1

    monkeypatch.setattr(
        apple.subprocess, "run", lambda argv, **kwargs: (calls.append(argv), Result())[1])
    monkeypatch.setattr(apple, "container_exists", lambda name: True)
    monkeypatch.setattr(apple.time, "sleep", lambda seconds: None)

    assert apple.delete_container("medulla-a") is False
    assert calls == [["container", "delete", "--force", "medulla-a"]] * 5
    assert "still exists" in capsys.readouterr().err


def test_interrupted_run_force_deletes_named_container(apple, monkeypatch):
    handlers = {}
    kills = []
    deletes = []
    scheduled = []

    class Thread:
        def join(self):
            pass

    class Proc:
        returncode = 130

        def wait(self):
            handlers[apple.signal.SIGINT](apple.signal.SIGINT, None)

    def install(signum, handler):
        previous = handlers.get(signum)
        handlers[signum] = handler
        return previous

    def spawn(*args, **kwargs):
        assert apple.signal.SIGINT in handlers
        assert apple.signal.SIGTERM in handlers
        return Proc()

    monkeypatch.setattr(apple.subprocess, "Popen", spawn)
    monkeypatch.setattr(apple.signal, "signal", install)
    monkeypatch.setattr(
        apple, "kill_container", lambda name, signal_name=None: kills.append((name, signal_name)))
    monkeypatch.setattr(apple, "delete_container", lambda name: deletes.append(name))
    monkeypatch.setattr(
        apple,
        "schedule_force_cleanup",
        lambda name, proc, wake, failed: (
            scheduled.append((name, proc, wake, failed)), Thread())[1],
    )
    monkeypatch.setattr(apple.shared, "_unlink_env_file", lambda: None)

    assert apple.run_apple("medulla:test", [], ["-w", "flow"]) == 130
    assert len(kills) == 1 and kills[0][1] == "INT"
    assert len(scheduled) == 1 and scheduled[0][0] == kills[0][0]
    assert deletes == []


def test_force_cleanup_after_grace_deletes_live_vm(apple, monkeypatch):
    events = []

    class Proc:
        def poll(self):
            return None

    class Wake:
        def wait(self, seconds):
            events.append(("wait", seconds))

    proc = Proc()
    failed = apple.threading.Event()
    monkeypatch.setattr(
        apple, "delete_container", lambda name: (events.append(("delete", name)), True)[1])
    monkeypatch.setattr(
        apple.shared,
        "terminate_process_group",
        lambda target, force: events.append(("terminate", target, force)),
    )

    apple.force_cleanup_after_grace("medulla-a", proc, Wake(), failed, 2)
    assert events == [
        ("wait", 2),
        ("terminate", proc, True),
        ("delete", "medulla-a"),
    ]
    assert not failed.is_set()


def test_force_cleanup_deletes_vm_even_if_client_already_exited(apple, monkeypatch):
    events = []

    class Proc:
        def poll(self):
            return 130

    class Wake:
        def wait(self, seconds):
            events.append(("wait", seconds))

    failed = apple.threading.Event()
    monkeypatch.setattr(
        apple, "delete_container", lambda name: (events.append(("delete", name)), True)[1])
    monkeypatch.setattr(
        apple.shared,
        "terminate_process_group",
        lambda target, force: events.append(("terminate", target, force)),
    )

    apple.force_cleanup_after_grace("medulla-a", Proc(), Wake(), failed, 2)
    assert events == [("wait", 2), ("delete", "medulla-a")]
    assert not failed.is_set()


def test_run_handlers_stay_installed_until_cleanup_finishes(apple, monkeypatch):
    handlers = {}
    handler_during_join = []

    class Proc:
        returncode = 130

        def wait(self):
            handlers[apple.signal.SIGINT](apple.signal.SIGINT, None)

    class Thread:
        def join(self):
            handler_during_join.append(callable(handlers[apple.signal.SIGINT]))

    def install(signum, handler):
        previous = handlers.get(signum)
        handlers[signum] = handler
        return previous

    monkeypatch.setattr(apple.signal, "signal", install)
    monkeypatch.setattr(apple.subprocess, "Popen", lambda *args, **kwargs: Proc())
    monkeypatch.setattr(apple, "kill_container", lambda *args: None)
    monkeypatch.setattr(
        apple, "schedule_force_cleanup", lambda name, proc, wake, failed: Thread())
    monkeypatch.setattr(apple.shared, "_unlink_env_file", lambda: None)

    assert apple.run_apple("medulla:test", [], ["-w", "flow"]) == 130
    assert handler_during_join == [True]


def test_run_forwards_sigterm_as_term(apple, monkeypatch):
    handlers = {}
    kills = []

    class Proc:
        returncode = 143

        def wait(self):
            handlers[apple.signal.SIGTERM](apple.signal.SIGTERM, None)

    class Thread:
        def join(self):
            pass

    def install(signum, handler):
        previous = handlers.get(signum)
        handlers[signum] = handler
        return previous

    monkeypatch.setattr(apple.signal, "signal", install)
    monkeypatch.setattr(apple.subprocess, "Popen", lambda *args, **kwargs: Proc())
    monkeypatch.setattr(
        apple, "kill_container", lambda name, signal_name=None: kills.append(signal_name))
    monkeypatch.setattr(
        apple, "schedule_force_cleanup", lambda name, proc, wake, failed: Thread())
    monkeypatch.setattr(apple.shared, "_unlink_env_file", lambda: None)

    assert apple.run_apple("medulla:test", [], ["-w", "flow"]) == 130
    assert kills == ["TERM"]


def test_run_cleanup_failure_is_not_reported_as_clean_interrupt(apple, monkeypatch):
    handlers = {}

    class Proc:
        returncode = 130

        def wait(self):
            handlers[apple.signal.SIGINT](apple.signal.SIGINT, None)

    class Thread:
        def __init__(self, failed):
            self.failed = failed

        def join(self):
            self.failed.set()

    def install(signum, handler):
        previous = handlers.get(signum)
        handlers[signum] = handler
        return previous

    monkeypatch.setattr(apple.signal, "signal", install)
    monkeypatch.setattr(apple.subprocess, "Popen", lambda *args, **kwargs: Proc())
    monkeypatch.setattr(apple, "kill_container", lambda *args: None)
    monkeypatch.setattr(
        apple, "schedule_force_cleanup",
        lambda name, proc, wake, failed: Thread(failed),
    )
    monkeypatch.setattr(apple.shared, "_unlink_env_file", lambda: None)

    assert apple.run_apple("medulla:test", [], ["-w", "flow"]) == 1


def test_main_fails_cleanly_off_macos(apple, monkeypatch, capsys):
    monkeypatch.setattr(apple.sys, "platform", "linux")
    assert apple.main() == 1
    assert "requires macOS" in capsys.readouterr().err


def test_main_rejects_intel_and_old_macos(apple, monkeypatch, capsys):
    monkeypatch.setattr(apple.sys, "platform", "darwin")
    monkeypatch.setattr(apple.platform, "machine", lambda: "x86_64")
    assert apple.main() == 1
    assert "Apple silicon" in capsys.readouterr().err

    monkeypatch.setattr(apple.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(apple.platform, "mac_ver", lambda: ("15.7", ("", "", ""), ""))
    assert apple.main() == 1
    assert "macOS 26" in capsys.readouterr().err


def test_main_reports_missing_container_cli(apple, monkeypatch, capsys):
    monkeypatch.setattr(apple.sys, "platform", "darwin")
    monkeypatch.setattr(apple.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(apple.platform, "mac_ver", lambda: ("26.0", ("", "", ""), ""))
    monkeypatch.setattr(apple.shutil, "which", lambda name: None)
    assert apple.main() == 1
    assert "CLI not found" in capsys.readouterr().err


def test_main_reports_stopped_container_service(apple, monkeypatch, capsys):
    class Result:
        returncode = 0
        stdout = '{"status":"stopped"}'

    monkeypatch.setattr(apple.sys, "platform", "darwin")
    monkeypatch.setattr(apple.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(apple.platform, "mac_ver", lambda: ("26.0", ("", "", ""), ""))
    monkeypatch.setattr(apple.shutil, "which", lambda name: "/usr/local/bin/container")
    monkeypatch.setattr(apple.subprocess, "run", lambda argv, **kwargs: Result())
    assert apple.main() == 1
    assert "container system start" in capsys.readouterr().err


def test_service_status_falls_back_for_older_cli(apple, monkeypatch):
    calls = []

    class Result:
        def __init__(self, returncode, stdout):
            self.returncode = returncode
            self.stdout = stdout

    def run(argv, **kwargs):
        calls.append(argv)
        if "--format" in argv:
            return Result(1, "unknown option")
        return Result(0, "status running")

    monkeypatch.setattr(apple.subprocess, "run", run)
    assert apple.container_service_running()
    assert calls == [
        ["container", "system", "status", "--format", "json"],
        ["container", "system", "status"],
    ]


def test_plain_service_status_rejects_not_running(apple):
    assert apple.plain_status_is_running("status running")
    assert apple.plain_status_is_running("apiserver is running")
    assert not apple.plain_status_is_running("status: not running")


def test_main_rejects_unparseable_macos_version(apple, monkeypatch, capsys):
    monkeypatch.setattr(apple.sys, "platform", "darwin")
    monkeypatch.setattr(apple.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(apple.platform, "mac_ver", lambda: ("unknown", ("", "", ""), ""))
    assert apple.main() == 1
    assert "macOS 26" in capsys.readouterr().err
