"""Two questions asked of a workflow before the container starts.

Both read the HARNESS FIELDS rather than the file text: `"agy" in yaml.read_text()`
sent the runner into the macOS Keychain because the word appeared in a comment, and
a substring match for `session:` would do the same for a prompt that mentions one.
"""
from __future__ import annotations

from pathlib import Path

from dockerlib.image import _config_yaml


def workflow_uses_agy(workflow: str | None) -> bool:
    """Does this workflow actually use the agy harness?

    Only then are the Keychain-extracted agy keys mounted: a Keychain prompt on every
    --docker run for workflows that never touch agy is noise, and scary noise.

    Reads the HARNESS FIELDS, not the file text. The previous check was
    `"agy" in yaml_path.read_text()` — a substring match, so the word appearing in a
    prompt, a comment or a model name sent the runner into the macOS Keychain for
    credentials the run never needed. Anything unreadable or unparseable keeps the old
    permissive answer: missing credentials fail confusingly, a spurious prompt is merely
    annoying.
    """
    if not workflow:
        return True
    yaml_path = _config_yaml(Path(workflow))
    try:
        import yaml
        data = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return True

    def mentions_agy(node) -> bool:
        if isinstance(node, dict):
            agent = node.get("agent")
            if isinstance(agent, str) and agent.strip() == "agy":
                return True
            if isinstance(agent, dict) and str(agent.get("harness", "")).strip() == "agy":
                return True
            # pool inputs carry the harness as data: {slug: gemini, harness: agy, ...}
            if str(node.get("harness", "")).strip() == "agy":
                return True
            return any(mentions_agy(v) for v in node.values())
        if isinstance(node, list):
            return any(mentions_agy(v) for v in node)
        return False

    return mentions_agy(data)

def workflow_names_a_session(workflow: str | None) -> bool:
    """Does this workflow name an agent session anywhere?

    If it does, the container must survive between nested runs: a conversation lives
    in the CLI's own state inside $HOME, so a fresh container is a fresh conversation
    no matter what id we hand it. Naming a session IS the request — no check for
    whether it is actually resumed, because that check would be one more thing to get
    wrong for no benefit. Unparseable or absent: no, keep today's --rm behaviour.
    """
    if not workflow:
        return False
    yaml_path = _config_yaml(Path(workflow))
    try:
        import yaml
        data = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return False

    def names_one(node) -> bool:
        if isinstance(node, dict):
            agent = node.get("agent")
            if isinstance(agent, dict) and str(agent.get("session", "")).strip():
                return True
            return any(names_one(v) for v in node.values())
        if isinstance(node, list):
            return any(names_one(v) for v in node)
        return False

    return names_one(data)
