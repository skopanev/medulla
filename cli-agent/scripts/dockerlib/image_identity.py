"""Version-bound identity and local verification for managed Docker images."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from importlib.metadata import distributions
from pathlib import Path

VERSION_LABEL = "org.medulla.engine-version"
REF_LABEL = "org.medulla.engine-ref"
_GIT_POPEN = subprocess.Popen


def _source_root() -> Path | None:
    root = Path(__file__).resolve().parents[3]
    return root if (root / "pyproject.toml").is_file() else None


def _project_version_text(document: str) -> str | None:
    in_project = False
    for raw in document.splitlines():
        line = raw.strip()
        if line.startswith("["):
            in_project = line == "[project]"
        elif in_project and line.startswith("version ="):
            return line.split("=", 1)[1].strip().strip("\"'") or None
    return None


def _commit(value: str | None) -> str | None:
    value = (value or "").strip().lower()
    return value if re.fullmatch(r"[0-9a-f]{7,64}", value) else None


def _git_output(root: Path, *args: str) -> str | None:
    try:
        process = _GIT_POPEN(
            ["git", "-C", str(root), *args], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
        )
        stdout, _ = process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        return None
    except (OSError, subprocess.SubprocessError):
        return None
    return stdout.strip() if process.returncode == 0 else None


def _source_identity(root: Path) -> tuple[str, str]:
    ref = _commit(_git_output(root, "rev-parse", "--verify", "HEAD^{commit}"))
    if not ref:
        raise SystemExit(
            f"error: cannot identify the medulla source commit at {root}; "
            "repair the checkout or use vars.IMAGE")
    document = _git_output(root, "show", f"{ref}:pyproject.toml")
    version = _project_version_text(document or "")
    if not version:
        raise SystemExit(
            f"error: commit {ref} has no Medulla project version; "
            "commit the version bump before building a Docker image")
    return version, ref


def _active_distribution():
    prefix = Path(sys.prefix).resolve()
    found = [
        dist for dist in distributions(name="medulla")
        if prefix in Path(dist.locate_file("")).resolve().parents
    ]
    if len(found) > 1:
        locations = sorted(str(Path(dist.locate_file("")).resolve()) for dist in found)
        raise SystemExit(
            f"error: multiple medulla distributions in active environment: {locations}")
    return found[0] if found else None


def _distribution_ref(dist) -> str | None:
    if not dist:
        return None
    try:
        direct = json.loads(dist.read_text("direct_url.json") or "{}")
        return _commit((direct.get("vcs_info") or {}).get("commit_id"))
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def _pipx_ref() -> str | None:
    metadata = Path(sys.prefix) / "pipx_metadata.json"
    try:
        package = json.loads(metadata.read_text(encoding="utf-8"))["main_package"]
        spec = package.get("package_or_url", "")
        return (_commit(spec.rsplit("@", 1)[-1])
                if "github.com/skopanev/medulla" in spec else None)
    except (OSError, KeyError, TypeError, ValueError):
        return None


def _stamp_ref() -> str | None:
    home = Path.home()
    stamps = (
        home / ".medulla" / "engine" / "INSTALLED_COMMIT",
        home / ".medulla-engine" / "INSTALLED_COMMIT",
    )
    for stamp in stamps:
        try:
            fields = stamp.read_text(encoding="utf-8").split(maxsplit=1)
        except OSError:
            fields = []
        ref = _commit(fields[0]) if fields else None
        if ref:
            return ref
    return None


def engine_identity() -> tuple[str, str]:
    """Resolve one consistent (version, immutable commit) snapshot."""
    source = _source_root()
    if source:
        return _source_identity(source)

    dist = _active_distribution()
    version = dist.version.strip() if dist and dist.version.strip() else ""
    if not version:
        raise SystemExit("error: cannot identify medulla version; run install.sh or pipx install medulla")
    ref = _distribution_ref(dist) or _pipx_ref() or _stamp_ref()
    if not ref:
        raise SystemExit(
            "error: cannot identify immutable medulla commit; run medulla upgrade "
            "or declare vars.IMAGE for a caller-owned image")
    return version, ref


def engine_version() -> str:
    return engine_identity()[0]


def engine_ref() -> str:
    return engine_identity()[1]


def tag_digest(dockerfile: Path, identity: tuple[str, str] | None = None) -> str:
    """Hash the recipe and exact engine identity: either change mints a tag."""
    version, ref = identity or engine_identity()
    content = dockerfile.read_bytes()
    material = content + b"\0medulla=" + version.encode() + b"@" + ref.encode()
    return hashlib.sha256(material).hexdigest()[:12]


def image_exists(image: str) -> bool | None:
    """Return None for inspection failure, False only for a confirmed missing image."""
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", image], capture_output=True, text=True,
            check=False, timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"error: cannot inspect Docker image '{image}': {exc}", file=sys.stderr)
        return None
    if result.returncode == 0:
        return True
    detail = result.stderr.strip()
    if "no such image" in detail.lower() or "no such object" in detail.lower():
        return False
    print(f"error: cannot inspect Docker image '{image}': {detail or 'docker failed'}", file=sys.stderr)
    return None


def build_flags(expected: str, ref: str) -> list[str]:
    """Give a managed Dockerfile the identity it must install and stamp itself."""
    return [
        "--build-arg", f"MEDULLA_VERSION={expected}",
        "--build-arg", f"MEDULLA_REF={ref}",
    ]


def _reported_identity(output: str) -> tuple[str, str | None]:
    match = re.fullmatch(
        r"medulla\s+(\S+)(?:\s+\(([0-9a-fA-F]{7,64})(?:\s+[^)]*)?\))?\s*",
        output,
    )
    return (match.group(1), _commit(match.group(2))) if match else ("<unavailable>", None)


def verify(image: str, expected: str, ref: str) -> bool:
    """Fail closed unless labels and the installed engine report one identity."""
    label_format = (f'{{{{ index .Config.Labels "{VERSION_LABEL}" }}}}'
                    f'\t{{{{ index .Config.Labels "{REF_LABEL}" }}}}')
    try:
        label = subprocess.run(
            ["docker", "image", "inspect", "--format", label_format, image],
            capture_output=True, text=True, check=False, timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"error: cannot read identity for image '{image}': {exc}", file=sys.stderr)
        return False
    actual_label = label.stdout.strip() if label.returncode == 0 else ""
    if actual_label != f"{expected}\t{ref}":
        shown = actual_label.replace("\t", "@") or "<missing>"
        print(
            f"error: image '{image}' is stale: identity says {shown}, "
            f"host expects {expected}@{ref}; rebuild required", file=sys.stderr,
        )
        return False

    try:
        probe = subprocess.run(
            ["docker", "run", "--rm", "--entrypoint", "medulla", image, "--version"],
            capture_output=True, text=True, check=False, timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"error: cannot probe medulla in image '{image}': {exc}", file=sys.stderr)
        return False
    actual_version, actual_ref = (
        _reported_identity(probe.stdout) if probe.returncode == 0
        else ("<unavailable>", None)
    )
    if (actual_version, actual_ref) != (expected, ref):
        detail = probe.stderr.strip()
        suffix = f": {detail}" if detail else ""
        print(
            f"error: image '{image}' contains medulla "
            f"{actual_version}@{actual_ref or '<missing>'}, host expects {expected}@{ref}; "
            "make the Dockerfile install and stamp MEDULLA_REF, or use vars.IMAGE "
            f"for an intentional pin{suffix}", file=sys.stderr,
        )
        return False
    return True
