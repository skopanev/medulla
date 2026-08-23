"""Load + validate + normalize workflow.yaml into the v2 model.

Every rejection here is E_VALIDATION — load-time, before any run dir exists.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from .errors import E_VALIDATION, EngineCrash
from .model import (
    BOOLEAN_TRAP_NAMES,
    CHANNEL_SIGNALS,
    DEFAULT_WORKFLOW_TIMEOUT,
    DEFAULTS_ALLOWED_KEYS,
    ENGINE_FACTS,
    ENV_BLACKLIST_EXACT,
    ENV_BLACKLIST_PREFIX,
    SIG_DONE,
    TERMINALS,
    Defaults,
    Node,
    Workflow,
)

DUNDER_RE = re.compile(r"^__.*__$")
VAR_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
# node names feed env suffixes (MEDULLA_MANIFEST_<NODE>) and step-dir paths —
# keep them env/filesystem-safe by construction
NODE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate mapping keys (PyYAML silently overwrites)."""


def _no_dup_mapping(loader, deep=False, node=None):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise EngineCrash(E_VALIDATION, f"duplicate YAML key: {key!r} (line {key_node.start_mark.line + 1})")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    lambda loader, node: _no_dup_mapping(loader, node=node),
)


_ERR_PATH: Path | None = None      # file being validated; set by load_workflow


def _err(msg: str) -> EngineCrash:
    # Always name the file. "workflow must be a YAML mapping" sent a whole day
    # chasing provider quotas: the offending file was a zero-byte workflow.yaml
    # nobody wrote by hand, and the message pointed at nothing.
    where = f" [{_ERR_PATH}]" if _ERR_PATH else ""
    return EngineCrash(E_VALIDATION, f"{msg}{where}")



# One node's worth of yaml — v2/contract_node.py. Re-exported: the suite and the older
# call sites have always reached them through this module.
from . import contract_node  # noqa: E402
from .contract_node import (  # noqa: E402,F401
    NODE_KEYS,
    _opt_int,
    _opt_str,
    _parse_action,
    _parse_agent,
    _parse_inputs,
    _parse_node,
)


def _validate_var_name(key: str, where: str) -> None:
    if not isinstance(key, str) or not VAR_NAME_RE.match(key):
        raise _err(f"{where}: invalid var name {key!r}")
    if key in ENV_BLACKLIST_EXACT or any(key.startswith(p) for p in ENV_BLACKLIST_PREFIX):
        raise _err(f"{where}: var name {key!r} is reserved (vars are exported to child env)")


def _validate_docker_block(raw) -> None:
    """Shape-check only — scripts/docker.py is the consumer and re-checks
    standalone (it runs before the engine and imports nothing from it)."""
    if raw is None:
        return
    if not isinstance(raw, dict):
        raise _err("docker: must be a mapping")
    unknown = set(raw) - {"shadow"}
    if unknown:
        raise _err(f"docker: unknown fields: {sorted(unknown)} (only 'shadow' exists)")
    shadow = raw.get("shadow")
    if shadow is None:
        return
    if not isinstance(shadow, list) or not all(isinstance(p, str) for p in shadow):
        raise _err("docker.shadow must be a list of workspace-relative paths")
    for p in shadow:
        parts = [s for s in p.split("/") if s not in ("", ".")]
        if not parts or p.startswith("/") or ".." in parts:
            raise _err("docker.shadow: path must stay inside the workspace "
                       f"(relative, no '..'): {p!r}")


def load_workflow(path: Path) -> Workflow:
    global _ERR_PATH
    contract_node._ERR_PATH = path      # the node parser names the file too
    path = Path(path)
    _ERR_PATH = path
    if not path.is_file():
        raise _err(f"workflow not found: {path}")
    try:
        data = yaml.load(path.read_text(encoding="utf-8"), Loader=_StrictLoader)
    except EngineCrash:
        raise
    except yaml.YAMLError as exc:
        raise _err(f"YAML parse error: {exc}")
    if not isinstance(data, dict):
        if data is None:
            raise _err("workflow file is EMPTY (0 bytes) — delete it and the "
                       "machine-wide definition applies again")
        raise _err(f"workflow must be a YAML mapping, got {type(data).__name__}")

    version = data.get("version")
    if version != "2":
        raise _err(
            f"version: \"2\" is required (got {version!r}). "
            f"This looks like a v1 workflow — see 'Migrating from v1' in README.md"
        )

    top_keys = {"version", "start", "vars", "timeout", "keep_runs", "defaults", "nodes",
                "docker"}
    unknown = set(data) - top_keys
    if unknown:
        raise _err(f"unknown top-level fields: {sorted(unknown)}")

    # docker: — host-side container policy (consumed by scripts/docker.py, the
    # engine only validates the shape). Law of the block: a workflow may only
    # SHRINK its container's exposure here, never enlarge it.
    _validate_docker_block(data.get("docker"))

    nodes_raw = data.get("nodes")
    if not isinstance(nodes_raw, dict) or not nodes_raw:
        raise _err("nodes must be a non-empty mapping")

    # defaults
    defaults_raw = data.get("defaults") or {}
    if not isinstance(defaults_raw, dict):
        raise _err("defaults must be a mapping")
    unknown = set(defaults_raw) - DEFAULTS_ALLOWED_KEYS
    if unknown:
        raise _err(f"defaults: unknown keys {sorted(unknown)} (flat policy scalars only)")
    d_fallback = None
    if defaults_raw.get("fallback") is not None:
        fb = defaults_raw["fallback"]
        if not isinstance(fb, dict):
            raise _err("defaults.fallback must be a mapping")
        d_fallback = _parse_action(fb, "defaults.fallback", allow_fallback=False,
                                   is_fallback=True)
        if d_fallback.kind != "agent":
            raise _err("defaults.fallback must be an agent action")
    d_on_signal = defaults_raw.get("on_signal") or {}
    if not isinstance(d_on_signal, dict):
        raise _err("defaults.on_signal must be a mapping")
    for sig, target in d_on_signal.items():
        if not isinstance(sig, str) or not isinstance(target, str):
            raise _err(f"defaults.on_signal: keys and targets must be strings ({sig!r})")
        if sig in CHANNEL_SIGNALS:
            raise _err(f"defaults.on_signal: '{sig}' is a channel signal and never routes")
        if DUNDER_RE.match(sig) and sig not in ENGINE_FACTS:
            raise _err(f"defaults.on_signal: unknown engine signal '{sig}'")
    d_ignore = defaults_raw.get("ignore_exit_code")
    if d_ignore is not None and not isinstance(d_ignore, bool):
        raise _err("defaults.ignore_exit_code must be a boolean")
    defaults = Defaults(
        timeout=_opt_int(defaults_raw.get("timeout"), "defaults.timeout"),
        max_attempts=_opt_int(defaults_raw.get("max_attempts"), "defaults.max_attempts"),
        ignore_exit_code=d_ignore,
        fallback=d_fallback,
        on_signal=dict(d_on_signal),
    )

    # nodes
    nodes: dict[str, Node] = {}
    for name, raw in nodes_raw.items():
        if not isinstance(name, str):
            raise _err(f"node name {name!r} is not a string (YAML boolean trap? quote it)")
        if name.lower() in BOOLEAN_TRAP_NAMES:
            raise _err(f"node name '{name}' is a YAML 1.1 boolean word — pick another")
        if DUNDER_RE.match(name):
            raise _err(f"node name '{name}' uses the engine namespace (__*__)")
        if not NODE_NAME_RE.match(name):
            raise _err(f"node name '{name}' must match [A-Za-z][A-Za-z0-9_-]* "
                       f"(it becomes env suffixes and paths)")
        nodes[name] = _parse_node(name, raw, f"node '{name}'")

    # graph checks
    start = data.get("start")
    if start not in nodes:
        raise _err(f"start node not found: {start!r}")
    for node in nodes.values():
        for sig, target in node.on_signal.items():
            if target not in nodes and target not in TERMINALS:
                raise _err(f"node '{node.name}': unknown target '{target}' for signal '{sig}'")
        if node.is_pool and SIG_DONE not in node.on_signal and SIG_DONE not in defaults.on_signal:
            raise _err(f"node '{node.name}': pool nodes must route {SIG_DONE} explicitly")
        # defaults-inherited self-edge = guaranteed loop (notify failing into notify)
        for sig, target in defaults.on_signal.items():
            if target == node.name and sig not in node.on_signal:
                raise _err(
                    f"node '{node.name}': defaults.on_signal['{sig}'] points at this node — "
                    f"override its engine facts explicitly (self-loop via defaults)"
                )
    for sig, target in defaults.on_signal.items():
        if target not in nodes and target not in TERMINALS:
            raise _err(f"defaults.on_signal: unknown target '{target}' for '{sig}'")

    # vars
    vars_raw = data.get("vars") or {}
    if not isinstance(vars_raw, dict):
        raise _err("vars must be a mapping")
    for key in vars_raw:
        _validate_var_name(key, "vars")
    vars_map = {k: str(v) for k, v in vars_raw.items()}

    # workflow timeout: 0 = unlimited
    t_raw = data.get("timeout", DEFAULT_WORKFLOW_TIMEOUT)
    if isinstance(t_raw, bool) or not isinstance(t_raw, int) or t_raw < 0:
        raise _err("timeout must be a non-negative integer (0 = unlimited)")
    timeout = None if t_raw == 0 else t_raw

    keep_runs = data.get("keep_runs", 20)
    if isinstance(keep_runs, bool) or not isinstance(keep_runs, int) or keep_runs < 1:
        raise _err("keep_runs must be a positive integer")

    return Workflow(
        version="2", start=start, nodes=nodes, vars=vars_map,
        timeout=timeout, keep_runs=keep_runs, defaults=defaults,
        path=path, dir=path.parent,
    )
