"""A package that exists in the tree but not in pyproject does not ship.

setuptools installs exactly what [tool.setuptools] packages lists — nothing is
inferred from the directory layout here. So every split that creates a new
subpackage is one line away from an installation that imports fine from a source
checkout (the suite loads by path) and dies with ModuleNotFoundError for everyone
who installed it. That has now happened twice: scripts/dockerlib, and v2/harnesses.
Twice is a test.
"""
from __future__ import annotations

import sys
from pathlib import Path

import tomllib

REPO = Path(__file__).resolve().parent.parent.parent
PYPROJECT = REPO / "pyproject.toml"


def _config() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["tool"]["setuptools"]


def _roots(cfg: dict) -> list[tuple[str, Path]]:
    """(package name, source dir) for every explicitly mapped root, longest first."""
    mapped = [(name, REPO / rel) for name, rel in cfg.get("package-dir", {}).items()]
    return sorted(mapped, key=lambda kv: -len(kv[0]))


def _expected(cfg: dict) -> set[str]:
    """Every importable subpackage reachable from a mapped root.

    A directory counts when it holds an __init__.py — that is what makes it a
    package rather than a folder of scripts, and it is the same test setuptools
    would apply if it were discovering instead of being told.
    """
    names: set[str] = set()
    for pkg, root in _roots(cfg):
        if not root.is_dir():
            continue
        for init in root.rglob("__init__.py"):
            rel = init.parent.relative_to(root)
            if any(part in {"__pycache__", "runs", "node_modules"} for part in rel.parts):
                continue
            names.add(pkg if rel == Path(".") else pkg + "." + ".".join(rel.parts))
    return names


def test_every_subpackage_in_the_tree_is_declared():
    cfg = _config()
    declared = set(cfg["packages"])
    missing = sorted(_expected(cfg) - declared)
    assert not missing, (
        "these packages exist on disk but pyproject does not ship them — "
        f"`pipx install` would raise ModuleNotFoundError: {missing}"
    )


def test_every_declared_package_exists():
    """The other direction: a stale name here is a build error, not a silent one,
    but it is cheaper to find in a test than in a release."""
    cfg = _config()
    roots = _roots(cfg)
    for pkg in cfg["packages"]:
        for name, root in roots:               # longest prefix wins
            if pkg == name or pkg.startswith(name + "."):
                rest = pkg[len(name):].strip(".")
                d = root / Path(*rest.split(".")) if rest else root
                assert d.is_dir(), f"{pkg} is declared but {d} does not exist"
                break
        else:
            raise AssertionError(f"{pkg} maps to no package-dir root")


def test_the_engine_imports_from_the_declared_set_only():
    """Guards the specific shape that broke twice: a module the engine imports at
    load time, living in a package nobody declared."""
    cfg = _config()
    declared = set(cfg["packages"])
    sys.path.insert(0, str(REPO / "cli-agent"))
    import medulla.v2.engine  # noqa: F401

    for mod in sorted(m for m in sys.modules if m.startswith("medulla.")):
        pkg = mod.rsplit(".", 1)[0] if "." in mod else mod
        if sys.modules[mod] is None or not hasattr(sys.modules[mod], "__file__"):
            continue
        if Path(sys.modules[mod].__file__ or "").name == "__init__.py":
            pkg = mod
        assert pkg in declared, f"{mod} lives in undeclared package {pkg}"
