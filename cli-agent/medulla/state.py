import os
from pathlib import Path

import yaml

VARS_PATH = Path(".medulla") / "vars.yaml"


def _vars_path() -> Path:
    """Resolve the vars file for this run.

    Keys off MEDULLA_TASK_ID (set by cli.py at startup) so parallel runs in
    one workdir get isolated state files. Falls back to the legacy shared
    .medulla/vars.yaml when no task id is present (manual module import).
    """
    tid = os.environ.get("MEDULLA_TASK_ID", "").strip()
    if tid:
        return Path(".medulla") / f"vars.{tid}.yaml"
    return VARS_PATH


def load_vars() -> dict[str, str]:
    path = _vars_path()
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return {k: str(v) for k, v in (data or {}).items()}


def save_var(key: str, value: str) -> None:
    path = _vars_path()
    data = load_vars()
    data[key] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False), encoding="utf-8")


def delete_var(key: str) -> None:
    path = _vars_path()
    data = load_vars()
    if key in data:
        del data[key]
        if data:
            path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False), encoding="utf-8")
        elif path.is_file():
            path.unlink()


def clear_vars() -> None:
    path = _vars_path()
    if path.is_file():
        path.unlink()


def export_vars() -> None:
    for key, value in load_vars().items():
        os.environ[key] = value
