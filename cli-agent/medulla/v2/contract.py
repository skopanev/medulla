"""Load + validate + normalize pipeline.yaml into the v2 model.

Every rejection here is E_VALIDATION — load-time, before any run dir exists.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from .errors import EngineCrash, E_VALIDATION
from .model import (
    Action, AgentSpec, Defaults, InputsSpec, Node, Pipeline, Pool,
    BOOLEAN_TRAP_NAMES, CHANNEL_SIGNALS, DEFAULTS_ALLOWED_KEYS, ENGINE_FACTS,
    ENV_BLACKLIST_EXACT, ENV_BLACKLIST_PREFIX, SIG_DONE, TERMINALS,
    DEFAULT_PIPELINE_TIMEOUT, DEFAULT_SOURCE_TIMEOUT,
)

DUNDER_RE = re.compile(r"^__.*__$")
VAR_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


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


def _err(msg: str) -> EngineCrash:
    return EngineCrash(E_VALIDATION, msg)


def _parse_agent(raw, where: str) -> AgentSpec:
    if isinstance(raw, str):  # scalar shortcut: agent: codex
        if not raw.strip():
            raise _err(f"{where}: agent name is empty")
        return AgentSpec(harness=raw.strip())
    if isinstance(raw, dict):
        harness = raw.get("harness")
        if not isinstance(harness, str) or not harness.strip():
            raise _err(f"{where}: agent.harness is required and must be a string")
        unknown = set(raw) - {"harness", "model", "effort", "args"}
        if unknown:
            raise _err(f"{where}: unknown agent fields: {sorted(unknown)}")
        args = raw.get("args", [])
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise _err(f"{where}: agent.args must be a list of strings")
        return AgentSpec(
            harness=harness.strip(),
            model=_opt_str(raw.get("model"), f"{where}: agent.model"),
            effort=_opt_str(raw.get("effort"), f"{where}: agent.effort"),
            args=args,
        )
    raise _err(f"{where}: agent must be a string (harness shortcut) or a mapping")


def _opt_str(v, where: str) -> str | None:
    if v is None:
        return None
    if not isinstance(v, str):
        raise _err(f"{where} must be a string")
    return v


def _opt_int(v, where: str, minimum: int = 1) -> int | None:
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int):
        raise _err(f"{where} must be an integer")
    if v < minimum:
        raise _err(f"{where} must be >= {minimum}")
    return v


def _parse_action(raw: dict, where: str, allow_fallback: bool = True) -> Action:
    has_shell = "shell" in raw
    has_agent = "agent" in raw
    if has_shell == has_agent:
        raise _err(f"{where}: exactly one of 'shell' / 'agent' is required")
    if has_shell and "prompt" in raw:
        raise _err(f"{where}: 'prompt' belongs to agent actions only")
    if has_shell and (not isinstance(raw["shell"], str) or not raw["shell"].strip()):
        raise _err(f"{where}: shell must be a non-empty string")

    fallback = None
    if raw.get("fallback") is not None:
        if not allow_fallback:
            raise _err(f"{where}: a fallback has no fallback")
        if has_shell:
            raise _err(f"{where}: fallback is meaningless for shell actions")
        fb = raw["fallback"]
        if not isinstance(fb, dict):
            raise _err(f"{where}: fallback must be a mapping")
        fb_action = _parse_action(fb, f"{where}.fallback", allow_fallback=False)
        if fb_action.kind != "agent":
            raise _err(f"{where}: fallback must be an agent action")
        fallback = fb_action

    ignore = raw.get("ignore_exit_code")
    if ignore is not None and not isinstance(ignore, bool):
        raise _err(f"{where}: ignore_exit_code must be a boolean")

    return Action(
        shell=raw.get("shell"),
        agent=_parse_agent(raw["agent"], where) if has_agent else None,
        prompt=_opt_str(raw.get("prompt"), f"{where}: prompt"),
        timeout=_opt_int(raw.get("timeout"), f"{where}: timeout"),
        max_attempts=_opt_int(raw.get("max_attempts"), f"{where}: max_attempts"),
        ignore_exit_code=ignore,
        fallback=fallback,
    )


def _parse_inputs(raw, where: str) -> InputsSpec:
    if isinstance(raw, list):
        kinds = {"object" if isinstance(x, dict) else "array" if isinstance(x, list) else "scalar" for x in raw}
        if "array" in kinds:
            raise _err(f"{where}: array inputs are forbidden (wrap in an object)")
        if len(kinds) > 1:
            raise _err(f"{where}: inputs must be one kind (all scalars or all objects)")
        return InputsSpec(data=raw)
    if isinstance(raw, dict):
        if "shell" not in raw or not isinstance(raw["shell"], str):
            raise _err(f"{where}: inputs source must be {{shell: \"cmd\"}}")
        if "format" in raw:
            raise _err(f"{where}: 'format' is reserved and not implemented yet — sniffing decides")
        unknown = set(raw) - {"shell", "timeout"}
        if unknown:
            raise _err(f"{where}: unknown inputs fields: {sorted(unknown)}")
        return InputsSpec(
            shell=raw["shell"],
            shell_timeout=_opt_int(raw.get("timeout"), f"{where}: inputs.timeout") or DEFAULT_SOURCE_TIMEOUT,
        )
    if isinstance(raw, str):
        raise _err(
            f"{where}: a bare string is ambiguous — wrap data in a list, or a command in {{shell: ...}}"
        )
    raise _err(f"{where}: inputs must be a list (data) or {{shell: ...}} (source)")


NODE_KEYS = {
    "shell", "agent", "prompt", "timeout", "max_attempts", "ignore_exit_code", "fallback",
    "inputs", "max_parallel", "min_success", "pre", "post", "on_signal",
}


def _parse_node(name: str, raw: dict, where: str) -> Node:
    if not isinstance(raw, dict):
        raise _err(f"{where}: node must be a mapping")
    unknown = set(raw) - NODE_KEYS
    if unknown:
        raise _err(f"{where}: unknown fields: {sorted(unknown)}")

    action = _parse_action(raw, where)

    pool = None
    if "inputs" in raw:
        mp_raw = raw.get("max_parallel", 1)
        if mp_raw == "all":
            max_parallel = None
        else:
            max_parallel = _opt_int(mp_raw, f"{where}: max_parallel")
        ms_raw = raw.get("min_success", "all")
        if ms_raw == "all":
            min_success = None
        else:
            min_success = _opt_int(ms_raw, f"{where}: min_success")
        if action.ignore_exit_code:
            raise _err(f"{where}: ignore_exit_code is forbidden in pool nodes — min_success owns that role")
        pool = Pool(inputs=_parse_inputs(raw["inputs"], f"{where}: inputs"),
                    max_parallel=max_parallel, min_success=min_success)
    else:
        for key in ("max_parallel", "min_success"):
            if key in raw:
                raise _err(f"{where}: {key} requires inputs")

    on_signal = raw.get("on_signal", {})
    if not isinstance(on_signal, dict):
        raise _err(f"{where}: on_signal must be a mapping")
    for sig, target in on_signal.items():
        if not isinstance(sig, str):
            raise _err(f"{where}: on_signal key {sig!r} is not a string (YAML boolean trap? quote it)")
        if sig in CHANNEL_SIGNALS:
            raise _err(f"{where}: '{sig}' is a channel signal and never routes")
        if not isinstance(target, str):
            raise _err(f"{where}: target for '{sig}' must be a plain string")
        if DUNDER_RE.match(sig) and sig not in ENGINE_FACTS:
            raise _err(f"{where}: unknown engine signal '{sig}'")
        if pool is not None and not DUNDER_RE.match(sig):
            raise _err(
                f"{where}: '{sig}' — pool bodies' signals never route (law of layers); "
                f"only engine facts are routable on pool nodes"
            )

    for hook in ("pre", "post"):
        if raw.get(hook) is not None and not isinstance(raw[hook], str):
            raise _err(f"{where}: {hook} must be a shell string")

    return Node(name=name, action=action, pool=pool,
                pre=raw.get("pre"), post=raw.get("post"), on_signal=dict(on_signal))


def _validate_var_name(key: str, where: str) -> None:
    if not isinstance(key, str) or not VAR_NAME_RE.match(key):
        raise _err(f"{where}: invalid var name {key!r}")
    if key in ENV_BLACKLIST_EXACT or any(key.startswith(p) for p in ENV_BLACKLIST_PREFIX):
        raise _err(f"{where}: var name {key!r} is reserved (vars are exported to child env)")


def load_pipeline(path: Path) -> Pipeline:
    path = Path(path)
    if not path.is_file():
        raise _err(f"pipeline not found: {path}")
    try:
        data = yaml.load(path.read_text(encoding="utf-8"), Loader=_StrictLoader)
    except EngineCrash:
        raise
    except yaml.YAMLError as exc:
        raise _err(f"YAML parse error: {exc}")
    if not isinstance(data, dict):
        raise _err("pipeline must be a YAML mapping")

    version = data.get("version")
    if version != "2":
        raise _err(
            f"version: \"2\" is required (got {version!r}). "
            f"This looks like a v1 pipeline — see 'Migrating from v1' in README.md"
        )

    top_keys = {"version", "start", "vars", "timeout", "keep_runs", "defaults", "nodes"}
    unknown = set(data) - top_keys
    if unknown:
        raise _err(f"unknown top-level fields: {sorted(unknown)}")

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
        d_fallback = _parse_action(fb, "defaults.fallback", allow_fallback=False)
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

    # pipeline timeout: 0 = unlimited
    t_raw = data.get("timeout", DEFAULT_PIPELINE_TIMEOUT)
    if isinstance(t_raw, bool) or not isinstance(t_raw, int) or t_raw < 0:
        raise _err("timeout must be a non-negative integer (0 = unlimited)")
    timeout = None if t_raw == 0 else t_raw

    keep_runs = data.get("keep_runs", 20)
    if isinstance(keep_runs, bool) or not isinstance(keep_runs, int) or keep_runs < 1:
        raise _err("keep_runs must be a positive integer")

    return Pipeline(
        version="2", start=start, nodes=nodes, vars=vars_map,
        timeout=timeout, keep_runs=keep_runs, defaults=defaults,
        path=path, dir=path.parent,
    )
