"""Two-phase render. Law: files are code, values are data.

Phase 1: {{file:path}} inclusion — recursive (depth <= 10, exceeding = error with the
chain), relative paths resolve against the INCLUDING file's directory, paths static.
Phase 2: ONE simultaneous pass over the expanded text for var/input/last tokens.
Substituted values are never re-scanned (inert — injection-safe by construction).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .errors import EngineCrash, E_RENDER

MAX_INCLUDE_DEPTH = 10

FILE_RE = re.compile(r"\{\{file:([^}]+)\}\}")
# one alternation = one simultaneous pass; values stay inert
TOKEN_RE = re.compile(
    r"\{\{var:(?P<var>[A-Za-z][A-Za-z0-9_]*)(?::-(?P<vdef>[^}]*))?\}\}"
    r"|\{\{input(?:\.(?P<ipath>[A-Za-z0-9_.]+))?(?::-(?P<idef>[^}]*))?\}\}"
    r"|\{\{input_index\}\}"
    r"|\{\{input_count\}\}"
    r"|\{\{last\.(?P<last>node|signal|message|rc)\}\}"
)


class RenderError(Exception):
    """Raised for broken templates/data. The engine maps it to E_RENDER (decision node)
    or to a per-input failure (pool input) — the split lives in the engine, not here."""


def expand_files(text: str, base_dir: Path, _depth: int = 0, _chain: tuple[str, ...] = ()) -> str:
    if _depth > MAX_INCLUDE_DEPTH:
        raise RenderError(f"file inclusion deeper than {MAX_INCLUDE_DEPTH}: {' -> '.join(_chain)}")

    def repl(m: re.Match) -> str:
        rel = m.group(1).strip()
        target = (base_dir / rel).resolve()
        if not target.is_file():
            raise RenderError(f"included file not found: {rel} (from {base_dir})")
        content = target.read_text(encoding="utf-8")
        return expand_files(content, target.parent, _depth + 1, _chain + (rel,))

    return FILE_RE.sub(repl, text)


def _walk(value, dotted: str):
    cur = value
    for part in dotted.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None, False
    return cur, True


def _scalar(v) -> str:
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False, separators=(",", ":"))
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def render(
    text: str,
    base_dir: Path,
    vars_map: dict[str, str],
    input_value=None,
    has_input: bool = False,
    input_index: int | None = None,
    input_count: int | None = None,
    last: dict | None = None,
) -> str:
    """Render a code field (node prompt/shell/hook). Raises RenderError on broken refs."""
    text = expand_files(text, base_dir)

    def repl(m: re.Match) -> str:
        if m.group("var") is not None:
            key, default = m.group("var"), m.group("vdef")
            value = vars_map.get(key)
            if value is None or value == "":
                return default if default is not None else ""
            return value
        if m.group("last") is not None:
            return str((last or {}).get(m.group("last"), ""))
        token = m.group(0)
        if token == "{{input_index}}":
            if input_index is None:
                raise RenderError("{{input_index}} outside a pool")
            return str(input_index)
        if token == "{{input_count}}":
            if input_count is None:
                raise RenderError("{{input_count}} outside a pool")
            return str(input_count)
        # {{input}} family
        if not has_input:
            raise RenderError(f"{token} used but the node has no inputs")
        dotted, default = m.group("ipath"), m.group("idef")
        if dotted is None:
            return _scalar(input_value)
        if not isinstance(input_value, dict):
            raise RenderError(f"{{{{input.{dotted}}}}} on a scalar input")
        value, found = _walk(input_value, dotted)
        if not found or value is None:
            if default is not None:
                return default
            raise RenderError(f"input field '{dotted}' is missing and has no default")
        return _scalar(value)

    return TOKEN_RE.sub(repl, text)
