import re
from pathlib import Path

try:
    import yaml
except Exception:
    yaml = None

EXIT_STAGE = "__exit__"
NEXT_ITEM = "__next_item__"
FILE_TAG_RE = re.compile(r"\{\{file:([^}]+)\}\}")
VAR_TAG_RE = re.compile(r"\{\{var:([A-Za-z0-9_]+)(?::-(.*?))?\}\}")
LOOP_ITEM_TAG_RE = re.compile(r"\{\{__item__\}\}")
LOOP_LIST_ITEM_TAG_RE = re.compile(r"\{\{__list_item__\}\}")


def load_pipeline(path: Path) -> dict:
    if not path.is_file():
        raise FileNotFoundError(f"pipeline not found: {path}")
    if yaml is None:
        raise RuntimeError("missing dependency 'pyyaml' (install with: pip3 install pyyaml)")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("pipeline must be a YAML mapping")
    return data


def _validate_runner(runner: dict, name: str, label: str = "runner") -> None:
    if not isinstance(runner, dict):
        raise ValueError(f"stage '{name}' {label} must be a mapping")
    has_shell = "shell" in runner
    has_llm = "llm" in runner
    has_loop = "loop" in runner
    has_executor = "executor" in runner  # legacy
    if not has_shell and not has_llm and not has_loop and not has_executor:
        raise ValueError(f"stage '{name}' {label} must define 'shell', 'llm', or 'loop'")
    if sum([has_shell, has_llm, has_loop, has_executor]) > 1:
        raise ValueError(f"stage '{name}' {label} must define exactly one of 'shell', 'llm', 'loop', or 'executor'")
    if has_loop:
        loop = runner["loop"]
        if not isinstance(loop, dict):
            raise ValueError(f"stage '{name}' {label} loop must be a mapping")
        if "list" not in loop:
            raise ValueError(f"stage '{name}' {label} loop must define 'list'")
        inner = loop.get("runner")
        if inner is None:
            raise ValueError(f"stage '{name}' {label} loop must define 'runner'")
        _validate_runner(inner, name, f"{label} loop runner")


def _valid_target(target: str, stages: dict) -> bool:
    return target in (EXIT_STAGE, NEXT_ITEM) or target in stages


def _validate_signal_step(step: dict, name: str, sig: str, stages: dict) -> None:
    if "stage" in step:
        st = step["stage"]
        if not isinstance(st, str):
            raise ValueError(f"stage '{name}' signal '{sig}' stage must be string")
        if not _valid_target(st, stages):
            raise ValueError(f"stage '{name}' has invalid target '{st}' for signal '{sig}'")
    elif "runner" in step:
        _validate_runner(step["runner"], name, f"signal '{sig}' runner")
    elif "reset_iterations" in step:
        pass
    else:
        raise ValueError(f"stage '{name}' signal '{sig}' step must define 'runner', 'stage', or 'reset_iterations'")


def _resolve_stage_target(target):
    if isinstance(target, str):
        return target
    if isinstance(target, dict):
        return target.get("stage")
    if isinstance(target, list):
        for item in target:
            if isinstance(item, dict) and "stage" in item:
                return item["stage"]
    return None


def validate_pipeline(data: dict) -> None:
    if "starting" not in data or "stages" not in data:
        raise ValueError("pipeline must contain 'starting' and 'stages'")
    stages = data.get("stages")
    if not isinstance(stages, dict) or not stages:
        raise ValueError("stages must be a non-empty mapping")
    start = data.get("starting")
    if start not in stages:
        raise ValueError(f"starting stage not found: {start}")
    pipeline_fallback = data.get("fallback_runner")
    if pipeline_fallback is not None:
        _validate_runner(pipeline_fallback, "pipeline", "fallback_runner")
    for name, stage in stages.items():
        if not isinstance(stage, dict):
            raise ValueError(f"stage '{name}' must be a mapping")
        runner = stage.get("runner")
        if runner is None:
            raise ValueError(f"stage '{name}' must define runner")
        _validate_runner(runner, name)
        fallback = stage.get("fallback_runner")
        if fallback is not None:
            _validate_runner(fallback, name, "fallback_runner")
        on_signal = stage.get("on_signal")
        if not isinstance(on_signal, dict):
            raise ValueError(f"stage '{name}' must define on_signal mapping")
        for sig, target in on_signal.items():
            if isinstance(target, str):
                if not _valid_target(target, stages):
                    raise ValueError(f"stage '{name}' has invalid target '{target}' for signal '{sig}'")
            elif isinstance(target, list):
                has_stage = False
                for item in target:
                    if not isinstance(item, dict):
                        raise ValueError(f"stage '{name}' signal '{sig}' array items must be objects")
                    _validate_signal_step(item, name, sig, stages)
                    if "stage" in item:
                        has_stage = True
                if not has_stage:
                    raise ValueError(f"stage '{name}' signal '{sig}' array must contain a 'stage' item")
            elif isinstance(target, dict):
                stage_target = target.get("stage")
                if not isinstance(stage_target, str):
                    raise ValueError(f"stage '{name}' signal '{sig}' must define 'stage' as string")
                if not _valid_target(stage_target, stages):
                    raise ValueError(f"stage '{name}' has invalid target '{stage_target}' for signal '{sig}'")
            else:
                raise ValueError(f"stage '{name}' signal '{sig}' target must be string, object, or array")
        if isinstance(runner, dict) and "loop" in runner:
            has_next = False
            has_loop_done = False
            for sig, target in on_signal.items():
                resolved = _resolve_stage_target(target)
                if resolved == NEXT_ITEM:
                    has_next = True
                if sig == "loop_done":
                    has_loop_done = True
            if not has_next:
                raise ValueError(f"stage '{name}' has loop runner but no signal maps to '{NEXT_ITEM}'")
            if not has_loop_done:
                raise ValueError(f"stage '{name}' has loop runner but missing 'loop_done' in on_signal")


def render_text(text: str, base_dir: Path, vars_map: dict[str, str]) -> str:
    def expand_files(src: str, resolve_dir: Path, depth: int = 0) -> str:
        if depth > 10:
            return src

        def repl(m: re.Match) -> str:
            rel = m.group(1).strip()
            target = (resolve_dir / rel).resolve()
            content = target.read_text(encoding="utf-8")
            return expand_files(content, target.parent, depth + 1)

        return FILE_TAG_RE.sub(repl, src)

    text = expand_files(text, base_dir)

    def var_repl(m: re.Match) -> str:
        key = m.group(1)
        default = m.group(2) if m.group(2) is not None else ""
        value = vars_map.get(key)
        if value is None or value == "":
            return str(default)
        return str(value)

    text = VAR_TAG_RE.sub(var_repl, text)
    text = LOOP_ITEM_TAG_RE.sub(lambda m: str(vars_map.get("__item__", "")), text)
    text = LOOP_LIST_ITEM_TAG_RE.sub(lambda m: str(vars_map.get("__list_item__", "")), text)
    return text
