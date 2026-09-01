"""The generated image tag and baked engine version are one identity."""
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from dockerlib import image, image_identity  # noqa: E402


class Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def workflow(tmp_path):
    root = tmp_path / "demo"
    root.mkdir()
    (root / "workflow.yaml").write_text("start: done\nnodes: {}\n", encoding="utf-8")
    dockerfile = root / "Dockerfile"
    dockerfile.write_text("FROM alpine\n", encoding="utf-8")
    return root, dockerfile


def test_image_tag_changes_with_engine_version(tmp_path, monkeypatch):
    root, dockerfile = workflow(tmp_path)
    monkeypatch.setattr(image_identity, "engine_identity", lambda: ("4.58.0", "abc1234"))
    old = image.image_tag_for(str(root), dockerfile)
    monkeypatch.setattr(image_identity, "engine_identity", lambda: ("4.58.1", "abc1234"))
    new = image.image_tag_for(str(root), dockerfile)
    assert old != new
    assert old.startswith("medulla-demo:")
    assert new.startswith("medulla-demo:")


def test_image_tag_changes_with_engine_commit(tmp_path, monkeypatch):
    root, dockerfile = workflow(tmp_path)
    monkeypatch.setattr(image_identity, "engine_identity", lambda: ("4.58.1", "abc1234"))
    old = image.image_tag_for(str(root), dockerfile)
    monkeypatch.setattr(image_identity, "engine_identity", lambda: ("4.58.1", "def5678"))
    assert image.image_tag_for(str(root), dockerfile) != old


def test_engine_version_ignores_stale_cwd_metadata(tmp_path, monkeypatch):
    class Dist:
        def __init__(self, value, root):
            self.version = value
            self.root = root

        def locate_file(self, name):
            return self.root / name

    stale = Dist("4.27.1", tmp_path / "repo")
    active = Dist("4.56.3", Path(sys.prefix) / "lib" / "site-packages")
    monkeypatch.setattr(image_identity, "_source_root", lambda: None)
    monkeypatch.setattr(image_identity, "distributions", lambda **kwargs: [stale, active])
    monkeypatch.setattr(image_identity, "_stamp_ref", lambda: "abc1234")
    assert image_identity.engine_version() == "4.56.3"


def test_source_checkout_version_is_anchored_to_its_pyproject(tmp_path, monkeypatch):
    committed = '[project]\nname = "medulla"\nversion = "4.58.0"\n'
    (tmp_path / "pyproject.toml").write_text(committed.replace("4.58.0", "9.9.9"))
    monkeypatch.setattr(image_identity, "_source_root", lambda: tmp_path)
    monkeypatch.setattr(
        image_identity, "_git_output",
        lambda root, *args: "a" * 40 if args[0] == "rev-parse" else committed,
    )
    assert image_identity.engine_identity() == ("4.58.0", "a" * 40)


def test_real_active_environment_version_is_resolved():
    version, ref = image_identity.engine_identity()
    assert version.count(".") == 2
    assert len(ref) == 40 and all(char in "0123456789abcdef" for char in ref)


def test_pipx_metadata_supplies_immutable_ref(tmp_path, monkeypatch):
    metadata = {
        "main_package": {
            "package_or_url": "git+https://github.com/skopanev/medulla.git@abc1234"
        }
    }
    (tmp_path / "pipx_metadata.json").write_text(__import__("json").dumps(metadata))
    monkeypatch.setattr(image_identity, "_source_root", lambda: None)
    dist = type("Dist", (), {"version": "4.58.1", "read_text": lambda self, name: None})()
    monkeypatch.setattr(image_identity, "_active_distribution", lambda: dist)
    monkeypatch.setattr(image_identity.sys, "prefix", str(tmp_path))
    assert image_identity.engine_identity() == ("4.58.1", "abc1234")


def test_matching_image_identity_is_verified_without_network(monkeypatch):
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["docker", "image", "inspect"]:
            return Result(stdout="4.56.3\tabc1234\n")
        if cmd[:4] == ["docker", "run", "--rm", "--entrypoint"]:
            return Result(stdout="medulla 4.56.3  (abc1234)\n")
        raise AssertionError(cmd)

    monkeypatch.setattr(image_identity.subprocess, "run", run)
    assert image_identity.verify("medulla-demo:abc", "4.56.3", "abc1234")
    assert all("curl" not in cmd and "upgrade" not in cmd for cmd in calls)


@pytest.mark.parametrize(
    ("label", "binary", "reason"),
    [
        ("", None, "identity says <missing>"),
        ("4.56.2\tabc1234", None, "identity says 4.56.2@abc1234"),
        ("4.56.3\tabc1234", "medulla 4.56.2  (abc1234)\n", "contains medulla 4.56.2"),
        ("4.56.3\tabc1234", "medulla 4.56.3  (def5678)\n", "@def5678"),
    ],
)
def test_stale_image_fails_loudly(label, binary, reason, monkeypatch, capsys):
    def run(cmd, **kwargs):
        if cmd[:3] == ["docker", "image", "inspect"]:
            return Result(stdout=f"{label}\n")
        if binary is not None and cmd[:4] == ["docker", "run", "--rm", "--entrypoint"]:
            return Result(stdout=binary)
        raise AssertionError(cmd)

    monkeypatch.setattr(image_identity.subprocess, "run", run)
    assert not image_identity.verify("medulla-demo:abc", "4.56.3", "abc1234")
    assert reason in capsys.readouterr().err


def test_managed_build_carries_version_identity(tmp_path, monkeypatch):
    _, dockerfile = workflow(tmp_path)
    command = []
    verified = []

    class Proc:
        returncode = 0
        pid = 1

        def __init__(self, cmd, **kwargs):
            command.extend(cmd)

        def wait(self):
            return 0

    monkeypatch.setattr(image.subprocess, "Popen", Proc)
    monkeypatch.setattr(image, "prune_old_images", lambda *args: None)
    monkeypatch.setattr(image_identity, "engine_identity", lambda: ("4.56.3", "abc1234"))
    monkeypatch.setattr(
        image_identity,
        "verify",
        lambda name, expected, ref: verified.append((name, expected, ref)) or True,
    )

    assert image.ensure_image("medulla-demo:abc", True, None, {}, dockerfile=dockerfile) == 0
    version_at = command.index("MEDULLA_VERSION=4.56.3")
    assert command[version_at - 1] == "--build-arg"
    ref_at = command.index("MEDULLA_REF=abc1234")
    assert command[ref_at - 1] == "--build-arg"
    assert not any(arg.startswith("org.medulla.engine-") for arg in command)
    assert verified == [("medulla-demo:abc", "4.56.3", "abc1234")]


def test_existing_stale_managed_image_fails_before_launch(tmp_path, monkeypatch):
    _, dockerfile = workflow(tmp_path)
    monkeypatch.setattr(image_identity, "engine_identity", lambda: ("4.56.3", "abc1234"))
    monkeypatch.setattr(image_identity, "image_exists", lambda name: True)
    monkeypatch.setattr(image_identity, "verify", lambda *args: False)
    assert image.ensure_image("medulla-demo:abc", False, None, {}, dockerfile=dockerfile) == 1


def test_probe_timeout_fails_closed(monkeypatch, capsys):
    monkeypatch.setattr(
        image_identity.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(__import__("subprocess").TimeoutExpired("docker", 30)),
    )
    assert not image_identity.verify("medulla-demo:abc", "4.56.3", "abc1234")
    assert "cannot read identity" in capsys.readouterr().err


def test_installer_stamp_parses_commit_before_subject(tmp_path, monkeypatch):
    stamp = tmp_path / ".medulla" / "engine" / "INSTALLED_COMMIT"
    stamp.parent.mkdir(parents=True)
    stamp.write_text("b178272 feat: workflow timeout\n", encoding="utf-8")
    monkeypatch.setattr(image_identity.Path, "home", classmethod(lambda cls: tmp_path))
    assert image_identity._stamp_ref() == "b178272"


def test_source_ref_failure_never_falls_back_to_installed_copy(tmp_path, monkeypatch):
    monkeypatch.setattr(image_identity, "_source_root", lambda: tmp_path)
    monkeypatch.setattr(image_identity, "_git_output", lambda *args: None)
    monkeypatch.setattr(
        image_identity, "_active_distribution",
        lambda: pytest.fail("must not inspect another installation"),
    )
    with pytest.raises(SystemExit, match="source commit"):
        image_identity.engine_identity()


def test_ready_image_without_workflow_pulls_and_is_never_built(monkeypatch):
    calls = []
    monkeypatch.setattr(image_identity, "image_exists", lambda name: False)
    monkeypatch.setattr(image.subprocess, "run", lambda cmd, **kwargs: calls.append(cmd) or Result())
    assert image.ensure_image("vendor/pin:1", False, None, {}, ready_image=True) == 0
    assert calls == [["docker", "pull", "vendor/pin:1"]]
    with pytest.raises(SystemExit, match="--build is invalid with IMAGE"):
        image.ensure_image("vendor/pin:1", True, None, {}, ready_image=True)


def test_inspection_failure_does_not_build_or_pull(monkeypatch):
    monkeypatch.setattr(image_identity, "image_exists", lambda name: None)
    assert image.ensure_image("vendor/pin:1", False, None, {}, ready_image=True) == 1


def test_docker_without_workflow_or_ready_image_is_rejected(dockerpy, monkeypatch):
    monkeypatch.delenv("MEDULLA_IMAGE", raising=False)
    monkeypatch.setattr(sys, "argv", ["docker.py"])
    with pytest.raises(SystemExit, match="workflow required"):
        dockerpy.main()


def test_docker_ready_image_without_workflow_is_classified_as_ready(dockerpy, monkeypatch):
    seen = {}
    monkeypatch.setenv("MEDULLA_IMAGE", "vendor/pin:1")
    monkeypatch.setattr(sys, "argv", ["docker.py"])
    monkeypatch.setattr(dockerpy, "ensure_image", lambda *args, **kwargs: seen.update(kwargs) or 1)
    assert dockerpy.main() == 1
    assert seen["ready_image"] is True


def test_entrypoint_has_no_network_upgrade_and_keeps_ready_marker():
    script = (SCRIPTS / "init-docker.sh").read_text(encoding="utf-8")
    assert "raw.githubusercontent.com" not in script
    assert "medulla upgrade" not in script
    assert ": > /tmp/.medulla-ready" in script


def test_default_dockerfile_rejects_wrong_baked_version():
    dockerfile = (SCRIPTS / "Dockerfile.default").read_text(encoding="utf-8")
    assert ".medulla/engine/INSTALLED_COMMIT" in dockerfile
    assert "ARG MEDULLA_REF" in dockerfile
    assert "org.medulla.engine-version" in dockerfile
    assert "org.medulla.engine-ref" in dockerfile
    assert "medulla.git@${MEDULLA_REF}" in dockerfile
    assert '"$installed" = "$MEDULLA_VERSION"' in dockerfile
