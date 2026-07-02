"""medulla-graph CLI entry point — render a workflow pipeline as a graph."""

import sys
from pathlib import Path

from .graph import build_graph, open_file


def main() -> int:
    if len(sys.argv) < 3 or sys.argv[1] != "-w":
        print("usage: medulla-graph -w <workflow>")
        print("example: medulla-graph -w dev")
        return 1

    workflow = sys.argv[2]

    # Try .medulla/workflows first (after init), then cli-agent/workflows (dev)
    path = Path(".medulla") / "workflows" / workflow / "pipeline.yaml"
    if not path.is_file():
        # Source mode: cli-agent/medulla/graph_cli.py → cli-agent/workflows/
        path = Path(__file__).resolve().parent.parent / "workflows" / workflow / "pipeline.yaml"

    if not path.is_file():
        print(f"error: workflow not found: {workflow}")
        return 1

    output_path = build_graph(path)
    print(f"Generated: {output_path}")
    open_file(output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
