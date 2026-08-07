"""Select the container runtime encoded in an installed workflow skill."""
from __future__ import annotations

from pathlib import Path

_DOCKER_SKILL_RUN = "medulla --print-run-dir --docker"
_APPLE_SKILL_RUN = "medulla --print-run-dir --apple"
_DOCKER_LAUNCH = "medulla launch spar "
_APPLE_LAUNCH = "medulla launch spar --apple "
_HOST_SKILL_RUN = "medulla -w .medulla/workflows/"
_APPLE_HOST_SKILL_RUN = "medulla --apple -w .medulla/workflows/"
_DOCKER_SPAR_INIT = "medulla init spar --skill"
_APPLE_SPAR_INIT = "medulla init spar --skill --apple"


def skill_uses_apple(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8")
        return (_APPLE_SKILL_RUN in text or _APPLE_HOST_SKILL_RUN in text
                or _APPLE_LAUNCH in text)
    except OSError:
        return False


def path_has_symlink(path: Path) -> bool:
    current = Path(path.anchor) if path.is_absolute() else Path()
    for part in path.parts:
        if part == path.anchor:
            continue
        current /= part
        if current.is_symlink():
            return True
    return False


def set_skill_runtime(path: Path, apple: bool) -> None:
    text = path.read_text(encoding="utf-8")
    if apple:
        if (_APPLE_SKILL_RUN in text or _APPLE_HOST_SKILL_RUN in text
                or _APPLE_LAUNCH in text):
            updated = text
        elif _DOCKER_LAUNCH in text:
            updated = text.replace(_DOCKER_LAUNCH, _APPLE_LAUNCH)
        elif _DOCKER_SKILL_RUN in text:
            updated = text.replace(_DOCKER_SKILL_RUN, _APPLE_SKILL_RUN)
        elif _HOST_SKILL_RUN in text:
            updated = text.replace(_HOST_SKILL_RUN, _APPLE_HOST_SKILL_RUN)
        else:
            raise ValueError(f"SKILL.md has no Medulla run command: {path}")
        if _APPLE_SPAR_INIT not in updated:
            updated = updated.replace(_DOCKER_SPAR_INIT, _APPLE_SPAR_INIT)
    else:
        updated = text.replace(_APPLE_LAUNCH, _DOCKER_LAUNCH)
        updated = updated.replace(_APPLE_SKILL_RUN, _DOCKER_SKILL_RUN)
        updated = updated.replace(_APPLE_HOST_SKILL_RUN, _HOST_SKILL_RUN)
        updated = updated.replace(_APPLE_SPAR_INIT, _DOCKER_SPAR_INIT)
    if updated != text:
        path.write_text(updated, encoding="utf-8")


def set_skill_apple(path: Path) -> None:
    set_skill_runtime(path, apple=True)
