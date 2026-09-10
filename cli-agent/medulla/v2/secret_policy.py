"""Resolve the workflow's explicit credential grants before Docker starts."""
from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path

import yaml

from .workflow_path import resolve_workflow_yaml

POLICY_ENV = "MEDULLA_SECRET_POLICY"
_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class SecretPolicyError(ValueError):
    """The workflow cannot be given a bounded credential set."""


@lru_cache(maxsize=1)
def registry() -> dict:
    path = Path(__file__).with_name("credential_registry.json")
    return json.loads(path.read_text(encoding="utf-8"))


def validate_docker_block(raw, fail) -> None:
    """Shape-check host-side Docker policy; ``fail`` builds the caller's error."""
    if raw is None:
        return
    if not isinstance(raw, dict):
        raise fail("docker: must be a mapping")
    unknown = set(raw) - {"shadow", "secrets"}
    if unknown:
        raise fail(f"docker: unknown fields: {sorted(unknown)} (only 'shadow', 'secrets' exist)")
    shadow = raw.get("shadow")
    if shadow is not None:
        if not isinstance(shadow, list) or not all(isinstance(p, str) for p in shadow):
            raise fail("docker.shadow must be a list of workspace-relative paths")
        for path in shadow:
            parts = [part for part in path.split("/") if part not in ("", ".")]
            if not parts or path.startswith("/") or ".." in parts:
                raise fail("docker.shadow: path must stay inside the workspace "
                           f"(relative, no '..'): {path!r}")
    _validate_secrets(raw.get("secrets"), fail)


def _validate_secrets(raw, fail) -> None:
    if raw is None:
        return
    if not isinstance(raw, dict):
        raise fail("docker.secrets must be a mapping")
    unknown = set(raw) - {"harnesses", "grants"}
    if unknown:
        raise fail(f"docker.secrets: unknown fields: {sorted(unknown)}")
    harnesses = raw.get("harnesses", "auto")
    known = set(registry()["harnesses"]) - {"shell"}
    if harnesses != "auto":
        if not isinstance(harnesses, list) or not harnesses \
                or not all(isinstance(item, str) and item for item in harnesses):
            raise fail("docker.secrets.harnesses must be 'auto' or a non-empty list")
        unknown_harnesses = set(harnesses) - known
        if unknown_harnesses:
            raise fail(f"docker.secrets.harnesses: unknown harnesses: "
                       f"{sorted(unknown_harnesses)}")
    grants = raw.get("grants", {})
    if not isinstance(grants, dict):
        raise fail("docker.secrets.grants must be a mapping")
    known_bundles = set(registry()["bundles"])
    for harness, grant in grants.items():
        if harness not in registry()["harnesses"]:
            raise fail(f"docker.secrets.grants: unknown harness {harness!r}")
        if not isinstance(grant, dict) or set(grant) - {"env", "bundles"}:
            raise fail(f"docker.secrets.grants.{harness} must contain only env/bundles")
        env_names = grant.get("env", [])
        bundles = grant.get("bundles", [])
        if not isinstance(env_names, list) or not all(
                isinstance(name, str) and _ENV_RE.match(name) for name in env_names):
            raise fail(f"docker.secrets.grants.{harness}.env must be env names")
        if any(name.startswith("MEDULLA_") for name in env_names):
            raise fail(f"docker.secrets.grants.{harness}.env cannot grant MEDULLA_* names")
        if not isinstance(bundles, list) or not all(isinstance(name, str) for name in bundles):
            raise fail(f"docker.secrets.grants.{harness}.bundles must be bundle names")
        unknown_bundles = set(bundles) - known_bundles
        if unknown_bundles:
            raise fail(f"docker.secrets.grants.{harness}.bundles: unknown bundles: "
                       f"{sorted(unknown_bundles)}")


def resolve_policy(workflow: str | None, cli_vars: dict | None = None) -> dict:
    """Return the finite per-harness env/file policy for one workflow."""
    data = _read_workflow(workflow)
    docker = data.get("docker") or {}
    validate_docker_block(docker, SecretPolicyError)
    block = docker.get("secrets") or {}
    declared_vars = data.get("vars") if isinstance(data.get("vars"), dict) else {}
    vars_map = {**declared_vars, **(cli_vars or {})}   # the launcher's --var wins
    discovered, unresolved = _discover_harnesses(data, vars_map)
    declared = block.get("harnesses", "auto")
    if declared == "auto":
        selected = discovered
        if unresolved:
            # The harness only a run can know — a pool whose inputs a shell produces.
            # Refusing here stopped definitions that were never ambiguous to a reader:
            # the set they dispatch is written in the file, in the check that validates
            # it. Fall back to the names actually present, which bounds the grant by the
            # definition instead of by the registry.
            named = _harnesses_named_in(workflow)
            if not named:
                raise SecretPolicyError(
                    "agent harness cannot be resolved before docker run and no known "
                    "harness is named in the definition: give the var a literal default "
                    "in `vars:`, pass --var, or declare docker.secrets.harnesses")
            selected = discovered | named
    else:
        selected = set(declared)
        missing = discovered - selected
        if missing:
            raise SecretPolicyError(
                f"docker.secrets.harnesses omits workflow harnesses: {sorted(missing)}")
    known = set(registry()["harnesses"]) - {"shell"}
    unknown = selected - known
    if unknown:
        raise SecretPolicyError(f"unknown harnesses before docker run: {sorted(unknown)}")
    grants = block.get("grants", {})
    unused = set(grants) - selected - {"shell"}
    if unused:
        raise SecretPolicyError(f"credential grants name unused harnesses: {sorted(unused)}")

    contexts = selected | ({"shell"} if "shell" in grants else set())
    harness_policy = {}
    for harness in sorted(contexts):
        base = registry()["harnesses"][harness]
        extra = grants.get(harness, {})
        harness_policy[harness] = {
            "env": sorted(set(base["env"]) | set(extra.get("env", []))),
            "bundles": sorted(set(base["bundles"]) | set(extra.get("bundles", []))),
        }
    all_env = sorted({name for spec in harness_policy.values() for name in spec["env"]})
    bundles = sorted({name for spec in harness_policy.values() for name in spec["bundles"]})
    return {"version": 1, "harnesses": harness_policy,
            "all_env": all_env, "bundles": bundles}


def select_env_values(policy: dict, dotenv: dict, host_env=None) -> dict[str, str]:
    """Everything the CONTAINER gets: this run's harness credentials from the host,
    plus the merged .env whole.

    A business key is not a credential to declare. Shell nodes are what the .env is
    for, and an agent body never sees it — `env_keys_to_remove` strips it before the
    child starts. Demanding a declaration here asked the wrong question and offered a
    worse answer: the only way past the error was to grant a business key to codex.
    """
    allowed = set(policy["all_env"])
    source = os.environ if host_env is None else host_env
    values = {name: source[name] for name in allowed if source.get(name)}
    values.update(dotenv)
    if "GEMINI_API_KEY" in allowed and not values.get("GEMINI_API_KEY") \
            and values.get("GOOGLE_API_KEY"):
        values["GEMINI_API_KEY"] = values["GOOGLE_API_KEY"]
    return values


def encoded_policy(policy: dict) -> str:
    return json.dumps(policy, separators=(",", ":"), sort_keys=True)


def prepare_run_secrets(workflow, collect_dotenv, add_claude_fallback, cli_vars=None):
    """Resolve policy and values while keeping dotenv ownership in dockerlib."""
    policy = resolve_policy(workflow, cli_vars)
    dotenv = collect_dotenv(workflow)
    if "CLAUDE_CODE_OAUTH_TOKEN" in policy["all_env"]:
        add_claude_fallback(dotenv)
    selected = select_env_values(policy, dotenv)
    selected[POLICY_ENV] = encoded_policy(policy)
    return policy, selected


def env_keys_to_remove(harness: str, raw: str | None = None,
                       present=()) -> list[str]:
    """Names the agent body must not inherit — everything present that THIS harness
    was not granted.

    Two sources feed it. Rival harness credentials come from the run policy. The
    rest is `present`: names the engine merged into the body env itself
    (`_base_env` folds the whole .env into every body), which is how a business key
    reaches a model. The question is never "does medulla recognise this key" — a
    name it has never heard of is removed the same way. Shell bodies are untouched;
    the caller only asks for agents.
    """
    candidates = {str(name) for name in present}
    raw = os.environ.get(POLICY_ENV, "") if raw is None else raw
    if not raw:
        # Bare/non-Docker: no policy travels in the environment, but the engine still
        # knows what it merged and the registry still knows what this harness may
        # hold. Host system vars are not candidates, so PATH and HOME survive.
        allowed = set(registry()["harnesses"].get(harness, {}).get("env", []))
        rivals = {name for other, spec in registry()["harnesses"].items()
                  for name in spec["env"] if other != harness}
        return sorted((candidates | rivals) - allowed)
    try:
        policy = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return sorted(candidates | {name for spec in registry()["harnesses"].values()
                                    for name in spec["env"]})  # malformed fails closed
    allowed = set(policy.get("harnesses", {}).get(harness, {}).get("env", []))
    return sorted((candidates | set(policy.get("all_env", []))) - allowed)


def _harnesses_named_in(workflow: str | None) -> set[str]:
    """Registry harnesses whose names appear literally in the definition.

    The last resort before refusing a run whose harness only a shell command can
    produce. It cannot be wider than the registry and is usually NARROWER than
    granting every vendor: a definition that dispatches one harness names one.
    A name it misses is not a leak — that agent fails to authenticate and says so.
    """
    if not workflow:
        return set()
    try:
        text = resolve_workflow_yaml(Path(workflow)).read_text(encoding="utf-8")
    except Exception:
        return set()
    known = set(registry()["harnesses"]) - {"shell"}
    return {name for name in known
            if re.search(rf"\b{re.escape(name)}\b", text)}


def _read_workflow(workflow: str | None) -> dict:
    if not workflow:
        return {}
    path = resolve_workflow_yaml(Path(workflow))
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        raise SecretPolicyError(f"cannot read workflow secrets contract [{path}]: {exc}") from exc
    if not isinstance(data, dict):
        raise SecretPolicyError(f"workflow must be a mapping [{path}]")
    return data


_VAR_ONLY = re.compile(r"^\{\{\s*var:([A-Za-z_][A-Za-z0-9_]*)\s*\}\}$")


def _literal_var(text: str, vars_map: dict) -> str | None:
    """`{{var:HARNESS}}` whose value is already a literal is not dynamic.

    A default in the workflow's own `vars:` — or a `--var` the launcher just
    typed — is known before `docker run`, so refusing it as unresolvable made
    the common shape (one HARNESS var, one literal default) declare a list that
    only restated the file. Anything else — a var built from another var, a
    value arriving from a runtime signal — stays dynamic and still fails closed.
    """
    match = _VAR_ONLY.match(text)
    if not match:
        return None
    value = vars_map.get(match.group(1))
    if isinstance(value, str) and value.strip() and "{{" not in value:
        return value.strip()
    return None


def _discover_harnesses(data: dict, vars_map: dict | None = None) -> tuple[set[str], bool]:
    found: set[str] = set()
    unresolved = False
    vars_map = {} if vars_map is None else vars_map

    def visit(action, pool_harnesses: set[str] | None = None):
        nonlocal unresolved
        if not isinstance(action, dict):
            return
        agent = action.get("agent")
        harness = agent if isinstance(agent, str) else (
            agent.get("harness") if isinstance(agent, dict) else None)
        if isinstance(harness, str) and harness.strip():
            harness = harness.strip()
            if "{{" in harness:
                rendered = _literal_var(harness, vars_map)
                if rendered is not None:
                    found.add(rendered)
                elif pool_harnesses is None:
                    unresolved = True
                else:
                    found.update(pool_harnesses)
            else:
                found.add(harness)
        visit(action.get("fallback"), pool_harnesses)

    nodes = data.get("nodes") if isinstance(data.get("nodes"), dict) else {}
    for node in nodes.values():
        if not isinstance(node, dict):
            continue
        inputs = node.get("inputs")
        pool_harnesses = None
        if isinstance(inputs, list) and inputs and all(
                isinstance(row, dict) and isinstance(row.get("harness"), str)
                and row["harness"].strip() and "{{" not in row["harness"]
                for row in inputs):
            pool_harnesses = {row["harness"].strip() for row in inputs}
        visit(node, pool_harnesses)
    defaults = data.get("defaults") if isinstance(data.get("defaults"), dict) else {}
    visit(defaults.get("fallback"))
    return found, unresolved
