"""The YAML loader this contract uses, and the one rule it adds: no duplicate keys.

Split from contract.py under the project's 250-line rule ($MAX_LOC). PyYAML's default
is last-wins, silently — two `timeout:` keys in one node parse fine and one of them
does nothing. A workflow is code, and code with a silently ignored line is worse than
code that refuses to load.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from .errors import E_VALIDATION, EngineCrash


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


