"""Tailor bundled pool rosters to the agent CLIs installed on the host."""
from __future__ import annotations

import re
import shutil
from pathlib import Path

from .v2.harness import harness_binary

_INPUT = re.compile(r"^(?P<indent>\s*)-\s*\{(?P<body>[^}]*)\}(?P<tail>.*)$")
_DISABLED = re.compile(
    r"^(?P<indent>\s*)# medulla-disabled: "
    r"(?P<row>-\s*\{(?P<body>[^}]*)\}(?P<tail>.*))$"
)
_HARNESS = re.compile(r"(?:^|,)\s*harness:\s*['\"]?([A-Za-z0-9_-]+)")
_MIN_DECIDED = re.compile(r'^(?P<indent>\s*)MIN_DECIDED:\s*["\']?\d+["\']?')
_MIN_PANELISTS = 3


def filter_unavailable_harnesses(workflow_yaml: Path) -> list[str]:
    """Disable inline pool inputs whose canonical CLI is absent from host PATH.

    Bundled workflows keep comments and hand-authored formatting, so this edits only
    the compact input rows they ship and the matching pool's ``min_success`` line.
    Unknown/custom harnesses are left alone: medulla cannot infer their executable.
    """
    lines = workflow_yaml.read_text(encoding="utf-8").splitlines(keepends=True)
    removed: set[str] = set()
    pool_counts: list[tuple[int, int, int]] = []
    for index, line in enumerate(lines):
        if not re.match(r"^\s*inputs:\s*$", line):
            continue
        inputs_indent = len(line) - len(line.lstrip())
        active = 0
        pool_changed = False
        cursor = index + 1
        while cursor < len(lines):
            candidate = lines[cursor]
            text = candidate.rstrip("\n")
            match = _INPUT.match(text)
            disabled = _DISABLED.match(text)
            if not match and not disabled:
                break
            entry = match or disabled
            harness_match = _HARNESS.search(entry.group("body"))
            harness = harness_match.group(1) if harness_match else ""
            binary = harness_binary(harness)
            if binary and shutil.which(binary) is None:
                removed.add(harness)
                if match:
                    lines[cursor] = (
                        f"{match.group('indent')}# medulla-disabled: "
                        f"{candidate.lstrip()}"
                    )
                    pool_changed = True
            else:
                active += 1
                if disabled:
                    lines[cursor] = (
                        f"{disabled.group('indent')}{disabled.group('row')}\n"
                    )
                    pool_changed = True
            cursor += 1

        if active < _MIN_PANELISTS and cursor > index + 1:
            raise ValueError(
                f"at least {_MIN_PANELISTS} installed panelists are required; "
                f"found {active}"
            )
        if any(_DISABLED.match(lines[i].rstrip("\n"))
               for i in range(index + 1, cursor)) or pool_changed:
            pool_counts.append((cursor, active, inputs_indent))

    for start, active, inputs_indent in pool_counts:
        for index in range(start, len(lines)):
            stripped = lines[index].lstrip()
            indent = len(lines[index]) - len(stripped)
            if stripped.strip() and indent < inputs_indent:
                break
            if indent == inputs_indent and stripped.startswith("min_success:"):
                comment = ""
                if "#" in lines[index]:
                    comment = "  # strict majority of the deployed roster"
                lines[index] = f"{' ' * indent}min_success: {active // 2 + 1}{comment}\n"
                break

    if pool_counts:
        majority = min(active // 2 + 1 for _start, active, _indent in pool_counts)
        for index, line in enumerate(lines):
            match = _MIN_DECIDED.match(line)
            if match:
                lines[index] = f'{match.group("indent")}MIN_DECIDED: "{majority}"\n'
                break
        workflow_yaml.write_text(
            "".join(lines), encoding="utf-8",
        )
    return sorted(removed)
