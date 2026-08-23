"""Reading ONE node out of the yaml: its action, its agent, its inputs.

Split from contract.py under the project's 250-line rule ($MAX_LOC). contract.py keeps
the shape of a WORKFLOW — version, start, vars, the node map; this file knows what a
single node may say and what each field means. Every rejection here is a validation
error on purpose: a workflow that means something other than it says is worse than one
that refuses to load.
"""
from __future__ import annotations

import re

from .errors import E_VALIDATION, EngineCrash
from .model import (
    CHANNEL_SIGNALS,
    DEFAULT_SOURCE_TIMEOUT,
    ENGINE_FACTS,
    SANDBOX_LEVELS,
    Action,
    AgentSpec,
    InputsSpec,
    Node,
    Pool,
)

# The file being validated, so an error can name it. Set by contract.load_workflow
# before parsing starts — errors that only say WHAT is wrong send people hunting
# through every workflow they own for WHERE.
DUNDER_RE = re.compile(r"^__.*__$")
NODE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")

_ERR_PATH = None


def _err(msg: str) -> EngineCrash:
    where = f" [{_ERR_PATH}]" if _ERR_PATH else ""
    return EngineCrash(E_VALIDATION, f"{msg}{where}")


def _parse_agent(raw, where: str) -> AgentSpec:
    if isinstance(raw, str):  # scalar shortcut: agent: codex
        if not raw.strip():
            raise _err(f"{where}: agent name is empty")
        return AgentSpec(harness=raw.strip())
    if isinstance(raw, dict):
        harness = raw.get("harness")
        if not isinstance(harness, str) or not harness.strip():
            raise _err(f"{where}: agent.harness is required and must be a string")
        unknown = set(raw) - {"harness", "model", "effort", "sandbox", "args", "sets"}
        if unknown:
            raise _err(f"{where}: unknown agent fields: {sorted(unknown)}")
        args = raw.get("args", [])
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise _err(f"{where}: agent.args must be a list of strings")
        from .contract import VAR_NAME_RE  # local: contract.py imports this module
        sets = raw.get("sets", [])
        if not isinstance(sets, list) or not all(isinstance(k, str) for k in sets):
            raise _err(f"{where}: agent.sets must be a list of var names")
        bad = [k for k in sets if not VAR_NAME_RE.match(k)]
        if bad:
            raise _err(f"{where}: agent.sets: not var names: {bad}")
        sandbox = _opt_str(raw.get("sandbox"), f"{where}: agent.sandbox")
        # A literal level is checked here so a typo fails --validate, not mid-run.
        # A templated value (harness/model/effort do the same) can only resolve after
        # render, so it defers to the build-time check in harness._read_only.
        if sandbox and "{{" not in sandbox and sandbox not in SANDBOX_LEVELS:
            raise _err(f"{where}: agent.sandbox: {sandbox!r} is not one of {list(SANDBOX_LEVELS)}")
        return AgentSpec(
            harness=harness.strip(),
            model=_opt_str(raw.get("model"), f"{where}: agent.model"),
            effort=_opt_str(raw.get("effort"), f"{where}: agent.effort"),
            sandbox=sandbox,
            args=args,
            sets=[k.strip() for k in sets],
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


def _parse_action(raw: dict, where: str, allow_fallback: bool = True,
                  is_fallback: bool = False) -> Action:
    has_shell = "shell" in raw
    has_agent = "agent" in raw
    if has_shell == has_agent:
        raise _err(f"{where}: exactly one of 'shell' / 'agent' is required")
    # a fallback may omit prompt (inherits the primary's rendered text)
    if has_agent and not is_fallback and not isinstance(raw.get("prompt"), str):
        raise _err(f"{where}: agent actions require 'prompt'")
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
        fb_action = _parse_action(fb, f"{where}.fallback", allow_fallback=False,
                                  is_fallback=True)
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
        if raw.get(hook) is not None and (
            not isinstance(raw[hook], str) or not raw[hook].strip()
        ):
            raise _err(f"{where}: {hook} must be a non-empty shell string")

    return Node(name=name, action=action, pool=pool,
                pre=raw.get("pre"), post=raw.get("post"), on_signal=dict(on_signal))


