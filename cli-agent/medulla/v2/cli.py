"""v2 CLI — flag-based per the contract's Usage section:

  medulla -w <pipeline-dir> [--var K=V ...] [--node NAME]
  medulla -w <pipeline-dir> --resume | --run <dir>
  medulla -w <pipeline-dir> --validate | --dry-run
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .contract import load_pipeline
from .engine import find_resumable, run_pipeline
from .errors import EngineCrash


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse exits 2 on usage errors — but exit 2 means WORKFLOW FAILURE
        # in our contract. A bad flag is a CLI error: exit 1.
        self.print_usage(sys.stderr)
        print(f"error: {message}", file=sys.stderr)
        raise SystemExit(1)


def _resolve_pipeline_yaml(w: Path) -> Path:
    return w / "pipeline.yaml" if w.is_dir() else w


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(prog="medulla")
    parser.add_argument("-w", "--pipeline", required=True, type=Path,
                        help="pipeline directory (or pipeline.yaml path)")
    parser.add_argument("--var", action="append", default=[], metavar="KEY=VALUE")
    parser.add_argument("--node", default=None, help="start from a specific node (dev, fresh runs)")
    parser.add_argument("--resume", action="store_true", help="continue the latest resumable run")
    parser.add_argument("--run", type=Path, default=None, metavar="DIR",
                        help="continue a specific run directory")
    parser.add_argument("--validate", action="store_true", help="load + validate, no run")
    parser.add_argument("--dry-run", action="store_true", help="validate + print the plan, no run")
    ns = parser.parse_args(argv)

    yaml_path = _resolve_pipeline_yaml(ns.pipeline)

    if ns.validate or ns.dry_run:
        try:
            pipeline = load_pipeline(yaml_path)
        except EngineCrash as crash:
            print(f"{crash.code}: {crash.message}", file=sys.stderr)
            return 1
        if ns.dry_run:
            _print_plan(pipeline)
        else:
            print("ok")
        return 0

    resuming = ns.resume or ns.run is not None
    if ns.resume and ns.run is not None:
        parser.error("--resume and --run are mutually exclusive")
    if resuming and ns.var:
        parser.error("--var is for fresh runs only (a resumed run's vars live in vars.yaml)")
    if resuming and ns.node:
        parser.error("--node is for fresh runs only (resume continues from the journal)")

    cli_vars: dict[str, str] = {}
    for item in ns.var:
        if "=" not in item:
            parser.error(f"--var expects KEY=VALUE, got {item!r}")
        k, v = item.split("=", 1)
        cli_vars[k] = v

    resume_dir = None
    if ns.run is not None:
        resume_dir = ns.run
        if not (resume_dir / "pipeline.yaml").is_file():
            print(f"error: not a run directory: {resume_dir}", file=sys.stderr)
            return 1
    elif ns.resume:
        pdir = yaml_path.parent
        resume_dir = find_resumable(pdir)
        if resume_dir is None:
            print(f"error: no resumable run in {pdir / 'runs'}", file=sys.stderr)
            return 1

    return run_pipeline(yaml_path, cli_vars=cli_vars, start_override=ns.node,
                        resume_dir=resume_dir)


def _print_plan(pipeline) -> None:
    p = pipeline
    print(f"pipeline: {p.path}")
    print(f"start: {p.start}  timeout: {p.timeout or 'unlimited'}  keep_runs: {p.keep_runs}")
    if p.vars:
        print(f"vars: {', '.join(f'{k}={v}' for k, v in p.vars.items())}")
    for name, node in p.nodes.items():
        kind = "pool" if node.is_pool else "decision"
        if node.action.kind == "shell":
            body = (node.action.shell or "").strip().splitlines()[0][:60]
            action = f"shell: {body}"
        else:
            a = node.action.agent
            action = f"agent: {a.harness}" + (f" {a.model}" if a.model else "")
        print(f"- {name} [{kind}] {action}")
        if node.is_pool:
            pool = node.pool
            src = "list" if pool.inputs.data is not None else f"shell: {pool.inputs.shell}"
            mp = pool.max_parallel if pool.max_parallel is not None else "all"
            ms = pool.min_success if pool.min_success is not None else "all"
            print(f"    inputs: {src}  max_parallel: {mp}  min_success: {ms}")
        edges = dict(p.defaults.on_signal)
        edges.update(node.on_signal)
        for sig, target in edges.items():
            inherited = "" if sig in node.on_signal else "  (defaults)"
            print(f"    {sig} -> {target}{inherited}")


if __name__ == "__main__":
    raise SystemExit(main())
