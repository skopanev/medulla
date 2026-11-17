"""Apple runtime follows the shared mount and credential policy after a rebase."""
from types import SimpleNamespace

import pytest
from medulla.scripts import apple_build, apple_entry


def test_apple_uses_cli_harness_and_mounts_shared_definition_directory(
        tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    definition = tmp_path / "shared" / "workflow.yaml"
    definition.parent.mkdir()
    definition.write_text('''version: "2"
vars: {HARNESS: opencode}
start: work
nodes:
  work:
    agent: {harness: "{{var:HARNESS}}"}
    prompt: hi
    on_signal: {done: __exit_ok__}
''')
    monkeypatch.chdir(workspace)
    monkeypatch.setattr(apple_entry.sys, "platform", "darwin")
    monkeypatch.setattr(apple_entry.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(apple_entry.platform, "mac_ver", lambda: ("27.0", (), ""))
    monkeypatch.setattr(apple_entry.shutil, "which", lambda name: "/bin/container")
    monkeypatch.setattr(apple_entry, "container_service_running", lambda: True)
    monkeypatch.setattr(apple_entry, "ensure_image", lambda *args, **kwargs: 0)
    monkeypatch.setattr(apple_entry, "image_home", lambda image: "/home/medulla")
    monkeypatch.setattr(apple_entry.shared.dockermounts, "CONTAINER_HOME", "/home/hltm")
    monkeypatch.setattr(apple_entry.shared.dockerenv, "env_values_for_run", {})
    monkeypatch.setattr(apple_entry.shared.dockerpaths, "shadow_paths_for_run", [])
    monkeypatch.setattr(apple_entry.signal, "signal", lambda *args: None)
    monkeypatch.setattr(apple_entry.shared, "_collect_dotenv", lambda workflow: {})
    monkeypatch.setattr(apple_entry.shared, "_add_claude_token_fallback", lambda env: None)
    monkeypatch.setenv("MEDULLA_IMAGE", "fixture:latest")
    monkeypatch.setattr(
        apple_entry.sys, "argv",
        ["medulla", "-w", str(definition), "--var", "HARNESS=codex"],
    )
    seen = {}

    def build_volumes(*args, **kwargs):
        seen["bundles"] = kwargs["credential_bundles"]
        return []

    monkeypatch.setattr(apple_entry.shared, "build_volumes", build_volumes)
    monkeypatch.setattr(
        apple_entry.shared.mountpoints, "prepare", lambda *args: ([], 0))
    monkeypatch.setattr(
        apple_entry.shared.announce, "announce",
        lambda args, *rest: (args, "fixture-run"))

    def run_apple(image, volumes, args, **kwargs):
        seen["volumes"] = volumes
        seen["args"] = args
        return 0

    assert apple_entry.main(run_apple) == 0
    assert "codex" in seen["bundles"]
    assert "opencode" not in seen["bundles"]
    assert seen["volumes"] == [
        "-v", f"{definition.parent}:/mnt/medulla-workflows/shared:ro"]
    assert "/mnt/medulla-workflows/shared/workflow.yaml" in seen["args"]


def test_apple_home_probe_fails_instead_of_guessing_credential_destination(monkeypatch):
    monkeypatch.setattr(
        apple_build.subprocess, "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout=""))
    with pytest.raises(SystemExit, match="cannot determine HOME"):
        apple_build.image_home("fixture:latest")


def test_apple_container_name_matches_the_run_for_wait(monkeypatch):
    from medulla.scripts import apple_container

    seen = []
    proc = SimpleNamespace(returncode=0, wait=lambda: None)
    monkeypatch.setattr(
        apple_container.subprocess, "Popen",
        lambda command, **kwargs: seen.append(command) or proc)
    monkeypatch.setattr(apple_container.signal, "signal", lambda *args: None)
    monkeypatch.setattr(apple_container, "docker_client_env", lambda *args: {})
    monkeypatch.setattr(apple_container, "forwarded_env_values", lambda: {})
    monkeypatch.setattr(apple_container.shared.dockerpaths, "shadow_paths_for_run", [])
    assert apple_container.run_apple(
        "fixture:latest", [], ["--version"], run_dir_name="lane:42") == 0
    assert seen[0][seen[0].index("--name") + 1] == "medulla-lane-42"
