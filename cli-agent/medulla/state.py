import os
from pathlib import Path

import yaml

VARS_PATH = Path(".medulla") / "vars.yaml"


def load_vars() -> dict[str, str]:
    if not VARS_PATH.is_file():
        return {}
    data = yaml.safe_load(VARS_PATH.read_text(encoding="utf-8"))
    return {k: str(v) for k, v in (data or {}).items()}


def save_var(key: str, value: str) -> None:
    data = load_vars()
    data[key] = value
    VARS_PATH.parent.mkdir(parents=True, exist_ok=True)
    VARS_PATH.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False), encoding="utf-8")


def delete_var(key: str) -> None:
    data = load_vars()
    if key in data:
        del data[key]
        if data:
            VARS_PATH.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False), encoding="utf-8")
        elif VARS_PATH.is_file():
            VARS_PATH.unlink()


def clear_vars() -> None:
    if VARS_PATH.is_file():
        VARS_PATH.unlink()


def export_vars() -> None:
    for key, value in load_vars().items():
        os.environ[key] = value
