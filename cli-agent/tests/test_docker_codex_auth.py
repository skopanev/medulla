"""Execute the entrypoint's Codex credential section with synthetic filesystem data."""
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "init-docker.sh"


def run_codex_init(tmp_path, *, broker=None, mount=True):
    container_home = tmp_path / "container-home"
    container_home.mkdir()
    source = tmp_path / "mounted-codex"
    if mount:
        source.mkdir()
        (source / "auth.json").write_text("synthetic host credentials")
    env = {"PATH": os.defpath, "MEDULLA_TEST_HOME": str(container_home),
           "MEDULLA_TEST_MOUNT": str(source)}
    if broker in {"current", "legacy"}:
        config = container_home / ".config" / ("broker" if broker == "current" else "hltm-broker") / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text('{"url":"https://broker.invalid","key":"synthetic"}')
    elif broker in {"explicit", "missing-explicit"}:
        config = tmp_path / "custom-broker.json"
        if broker == "explicit":
            config.write_text('{"url":"https://broker.invalid","key":"synthetic"}')
        env["BROKER_CONFIG"] = str(config)
    elif broker == "environment":
        env.update(BROKER_URL="https://broker.invalid", BROKER_KEY="synthetic")
    # Run the actual section, excluding other providers and the network upgrade.
    # Redirect its two fixed filesystem roots without changing the process HOME.
    section = SCRIPT.read_text().split("# Codex credentials", 1)[1].split("# OpenCode auth", 1)[0]
    section = "# Codex credentials" + section
    section = section.replace("$HOME", "${MEDULLA_TEST_HOME}").replace("/mnt/codex", "${MEDULLA_TEST_MOUNT}")
    subprocess.run(["bash", "-c", section], env=env, check=True, capture_output=True, text=True)
    return container_home, source


@pytest.mark.parametrize("broker", ["current", "legacy", "explicit", "missing-explicit", "environment"])
@pytest.mark.parametrize("mount", [True, False])
def test_broker_has_empty_private_home_and_never_copies_host_auth(tmp_path, broker, mount):
    container_home, source = run_codex_init(tmp_path, broker=broker, mount=mount)
    canonical = container_home / ".codex"
    assert canonical.is_dir()
    assert canonical.stat().st_mode & 0o777 == 0o700
    assert not (canonical / "auth.json").exists()
    if mount:
        assert (source / "auth.json").read_text() == "synthetic host credentials"


def test_without_broker_native_codex_still_receives_mounted_auth(tmp_path):
    container_home, source = run_codex_init(tmp_path)
    assert (container_home / ".codex" / "auth.json").read_text() == "synthetic host credentials"
    assert (source / "auth.json").read_text() == "synthetic host credentials"


def test_without_broker_or_mount_no_auth_directory_is_needed(tmp_path):
    container_home, _ = run_codex_init(tmp_path, mount=False)
    assert not (container_home / ".codex").exists()
