import os
import platform
import subprocess
from pathlib import Path

import graphviz

from .pipeline import load_pipeline, validate_pipeline, EXIT_STAGE


def _parse_runner(runner: dict) -> tuple[str, str | None]:
    if "shell" in runner:
        return "shell", None
    if "llm" in runner:
        llm = str(runner["llm"])
        if ":" in llm:
            executor, model = llm.split(":", 1)
        else:
            executor, model = llm, None
        if "model" in runner:
            model = runner["model"]
        return executor, model
    executor = runner.get("executor", "?")
    model = runner.get("model")
    return executor, model


def _resolve_signal_target(target) -> tuple[str, bool]:
    if isinstance(target, str):
        return target, False
    if isinstance(target, dict):
        return target.get("stage", EXIT_STAGE), bool(target.get("reset_iterations", False))
    if isinstance(target, list):
        for item in target:
            if isinstance(item, dict) and "stage" in item:
                return item["stage"], any(i.get("reset_iterations") for i in target if isinstance(i, dict))
        return EXIT_STAGE, False
    return EXIT_STAGE, False


def build_graph(config_path: Path, output: str | None = None) -> str:
    data = load_pipeline(config_path)
    validate_pipeline(data)

    dot = graphviz.Digraph(
        name="pipeline",
        format="png",
        graph_attr={
            "rankdir": "TB",
            "fontname": "Helvetica",
            "bgcolor": "white",
            "pad": "0.5",
            "dpi": "150",
        },
        node_attr={
            "shape": "box",
            "style": "rounded,filled",
            "fontname": "Helvetica",
            "fontsize": "11",
        },
        edge_attr={
            "fontname": "Helvetica",
            "fontsize": "9",
        },
    )

    start_stage = data["starting"]
    stages = data["stages"]

    dot.node("__start__", "", shape="point", width="0.2")
    dot.edge("__start__", start_stage)

    dot.node(EXIT_STAGE, "EXIT", shape="doubleoctagon", fillcolor="#dddddd", style="filled", fontsize="10")

    for name, stage in stages.items():
        runner = stage.get("runner", {})
        executor, model = _parse_runner(runner)
        runner_info = f"{executor}:{model}" if model else executor

        parts = [f"<B>{name}</B>", runner_info]
        fallback = stage.get("fallback_runner")
        if fallback:
            fb_exec, fb_model = _parse_runner(fallback)
            fb_info = f"{fb_exec}:{fb_model}" if fb_model else fb_exec
            parts.append(f"fb: {fb_info}")

        label = f"<{'<BR/>'.join(parts)}>"

        attrs: dict[str, str] = {}
        if executor == "shell":
            attrs["fillcolor"] = "#ddffdd"
        else:
            attrs["fillcolor"] = "#ddeeff"
        if name == start_stage:
            attrs["penwidth"] = "2"

        dot.node(name, label, **attrs)

    for name, stage in stages.items():
        on_signal = stage.get("on_signal", {})
        for signal, target in on_signal.items():
            target_stage, reset_iters = _resolve_signal_target(target)

            edge_attrs: dict[str, str] = {}
            if signal == "default":
                edge_attrs["style"] = "dashed"
                edge_attrs["color"] = "#999999"
                edge_attrs["fontcolor"] = "#999999"
            elif target_stage == EXIT_STAGE and signal in ("failed", "error"):
                edge_attrs["color"] = "#cc0000"
                edge_attrs["fontcolor"] = "#cc0000"
            if reset_iters:
                edge_attrs["style"] = "dashed,bold"

            dot.edge(name, target_stage, label=signal, **edge_attrs)

    if output is None:
        output = config_path.stem

    path = dot.render(output, cleanup=True)
    return path


def open_file(path: str) -> None:
    if os.environ.get("MEDULLA_DOCKER"):
        return
    system = platform.system()
    try:
        if system == "Darwin":
            subprocess.Popen(["open", path])
        elif system == "Linux":
            subprocess.Popen(["xdg-open", path])
    except FileNotFoundError:
        pass
