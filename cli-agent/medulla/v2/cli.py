"""v2 CLI (walking skeleton): run / validate."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .contract import load_pipeline
from .engine import run_pipeline
from .errors import EngineCrash


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="medulla-v2")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="run a pipeline")
    p_run.add_argument("pipeline", type=Path)
    p_run.add_argument("--var", action="append", default=[], metavar="KEY=VALUE")
    p_run.add_argument("--node", default=None, help="start from a specific node (dev)")

    p_val = sub.add_parser("validate", help="load + validate, no run")
    p_val.add_argument("pipeline", type=Path)

    ns = parser.parse_args(argv)

    if ns.cmd == "validate":
        try:
            load_pipeline(ns.pipeline)
        except EngineCrash as crash:
            print(f"{crash.code}: {crash.message}", file=sys.stderr)
            return 1
        print("ok")
        return 0

    cli_vars: dict[str, str] = {}
    for item in ns.var:
        if "=" not in item:
            print(f"--var expects KEY=VALUE, got {item!r}", file=sys.stderr)
            return 1
        k, v = item.split("=", 1)
        cli_vars[k] = v
    return run_pipeline(ns.pipeline, cli_vars=cli_vars, start_override=ns.node)


if __name__ == "__main__":
    raise SystemExit(main())
