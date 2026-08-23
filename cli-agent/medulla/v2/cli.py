"""v2 CLI — flag-based per the contract's Usage section:

  medulla -w <workflow-dir> [--var K=V ...] [--node NAME]
  medulla -w <workflow-dir> --resume | --run <dir>
  medulla -w <workflow-dir> --validate | --dry-run
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .contract import load_workflow
from .engine import find_resumable, run_workflow
from .errors import EngineCrash
from .workflow_path import resolve_workflow_yaml as _resolve_workflow_yaml
from .workflow_path import shared_workflows  # noqa: F401 (public: init.py)


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse exits 2 on usage errors — but exit 2 means WORKFLOW FAILURE
        # in our contract. A bad flag is a CLI error: exit 1.
        self.print_usage(sys.stderr)
        print(f"error: {message}", file=sys.stderr)
        raise SystemExit(1)


ENV_HELP = """\
new here? `medulla help` — the launch contract, plus the workflows that resolve on
this machine, each with the exact command. This page is the reference, not the start.

environment the engine provides to bodies and hooks (agents: read this, it is the API):

  always
    MEDULLA_RUN_ID          run id (settable from outside for correlation)
    MEDULLA_RUN_DIR         this run's directory; put deliverables in $MEDULLA_RUN_DIR/artifacts/
    <all workflow vars>     exported as-is, including <signal:var>-set ones
    MEDULLA_TIMEOUT_S       resolved (deadline-clamped) timeout of the current step, seconds
    MEDULLA_ATTEMPT_ID      unique attempt id: <step>.<p|f><n>  (e.g. 003.i2.p1)
    MEDULLA_HARNESS         "shell" or the harness name of the current body

  after the first transition
    MEDULLA_LAST_NODE / _SIGNAL / _MESSAGE / _RC
                            outcome of the previously completed node (pool: _RC is empty)
    MEDULLA_LAST_EVENT_JSON same as one JSON object

  after a pool node completes
    MEDULLA_MANIFEST_<NODE> path to its manifest.jsonl (dashes->underscores, uppercased);
                            rows: {index,key,input,ok,reason,signal,message,rc,timed_out,
                                   attempts,fallback,harness,model,vars,updates,signals,
                                   duration_s,log}

  inside a pool input
    MEDULLA_INPUT           the input (objects as compact JSON)
    MEDULLA_INPUT_INDEX     1-based position     MEDULLA_INPUT_COUNT  total
    MEDULLA_INPUT_KEY       stable identity <index>:<sha256[:16]> (idempotency key)
    MEDULLA_INPUT_<KEY>     each flat scalar field of an object input, uppercased

  post hook only
    MEDULLA_BODY_RC / MEDULLA_BODY_SIGNAL
                            the body attempt's exit code and its raw signal (if any)

docker (host-side, handled by scripts/docker.py before the engine starts):
    medulla --docker -w <dir> ...   run inside the workflow's image
    --build                         force a no-cache image rebuild
    --mount <dir> / --mount-rw <dir>  extra mounts under /workspace/<name>
    image resolution precedence:    MEDULLA_IMAGE env > --var IMAGE >
                                    vars.IMAGE > build from (--var DOCKERFILE >
                                    vars.DOCKERFILE > packaged default)

subcommands: init <name> [--skill] (deploy a bundled template or scaffold a new workflow; --skill registers it with Claude Code), upgrade

environment the engine reads:
    MEDULLA_RETRY_DELAY_S   pause between attempts / before fallback (default 2)
    MEDULLA_RUN_ID          pre-seed the run id
    MEDULLA_DOCKER=1        set by scripts/docker.py: container is the sandbox

signals (print on stdout, must start the line for plain-text harnesses):
    <signal:NAME>message</signal:NAME>      route the graph (decision) / record (pool)
    <signal:var key=K>value</signal:var>    set a workflow var (fold law applies)
    <signal:update>progress</signal:update> progress line, never routes
"""


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(prog="medulla", epilog=ENV_HELP,
                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-w", "--workflow", required=True, type=Path,
                        help="workflow directory (or workflow.yaml path)")
    parser.add_argument("--var", action="append", default=[], metavar="KEY=VALUE")
    parser.add_argument("--var-file", action="append", default=[], metavar="KEY=PATH",
                        help="set a var from a FILE — for prompts too long or too "
                             "multiline to survive a shell argument")
    parser.add_argument("--node", default=None, help="start from a specific node (dev, fresh runs)")
    parser.add_argument("--resume", action="store_true", help="continue the latest resumable run")
    parser.add_argument("--run", type=Path, default=None, metavar="DIR",
                        help="continue a specific run directory")
    parser.add_argument("--validate", action="store_true", help="load + validate, no run")
    parser.add_argument("--dry-run", action="store_true", help="validate + print the plan, no run")
    parser.add_argument("--version", action="store_true", help="print version + installed commit")
    parser.add_argument("--runs-folder", type=Path, metavar="DIR",
                        help="write this run's history under DIR instead of beside the "
                             "workflow (pairs with --cwd-ro: a panel that must not write "
                             "into the tree it reviews)")
    parser.add_argument("--cwd-ro", action="store_true",
                        help="mount the WORKING DIRECTORY read-only (--docker only; "
                             "requires --runs-folder). The rest of the container stays "
                             "writable — the entrypoint copies credentials into $HOME "
                             "and agents write sessions there — so anything a body "
                             "writes outside the workspace or the runs folder is lost "
                             "when the container goes")
    parser.add_argument("--print-run-dir", action="store_true",
                        help="print the run directory to stdout at start (scripting/backgrounded runs)")
    if argv and "--version" in argv:
        _print_version()
        return 0
    ns = parser.parse_args(argv)

    # scripts/docker.py consumes --cwd-ro before the engine starts, so seeing it here
    # means there is no container — and nothing to mount read-only.
    if ns.cwd_ro:
        parser.error("--cwd-ro only applies to --docker runs")

    yaml_path = _resolve_workflow_yaml(ns.workflow)

    if ns.validate or ns.dry_run:
        try:
            workflow = load_workflow(yaml_path)
        except EngineCrash as crash:
            print(f"{crash.code}: {crash.message}", file=sys.stderr)
            return 1
        if ns.dry_run:
            _print_plan(workflow)
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
    # One key, one source. Silent last-wins hides a real mistake: two flags disagreeing
    # about the same var means somebody expected the other one to win.
    for item in ns.var_file:
        if "=" not in item:
            parser.error(f"--var-file expects KEY=PATH, got {item!r}")
        key, _, raw = item.partition("=")
        key = key.strip()
        if not key:
            parser.error(f"--var-file has no key: {item!r}")
        if not raw.strip():
            # Path("") resolves to the CWD, which then reads as a directory far away
            # from here — name the real mistake at the point it was made.
            parser.error(f"--var-file {key}: no path given")
        path = Path(raw).expanduser()
        # A FIFO blocks read_text() forever, and a directory raises a puzzle rather than
        # an answer. Say which it is instead of hanging with nothing on stdout.
        if not path.is_file():
            parser.error(f"--var-file {key}: not a regular file: {path}")
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            parser.error(f"--var-file {key}: cannot read {path}: {exc}")
        # An EMPTY file is an empty question, and a panel answers it for ten minutes
        # before anyone notices. A shell variable that lost its content fails silently;
        # a file can be checked, so it is.
        if not text.strip():
            parser.error(f"--var-file {key}: {path} is empty")
        if key in cli_vars:
            parser.error(f"--var-file {key}: given twice")
        cli_vars[key] = text
    for item in ns.var:
        if "=" not in item:
            parser.error(f"--var expects KEY=VALUE, got {item!r}")
        k, v = item.split("=", 1)
        if k in cli_vars:
            parser.error(f"--var {k}: already set by another --var or --var-file")
        cli_vars[k] = v

    resume_dir = None
    if ns.run is not None:
        resume_dir = ns.run
        from .rundir import config_yaml
        if not config_yaml(resume_dir).is_file():
            print(f"error: not a run directory: {resume_dir}", file=sys.stderr)
            return 1
    elif ns.resume:
        pdir = yaml_path.parent
        resume_dir = find_resumable(pdir, ns.runs_folder)
        if resume_dir is None:
            root = ns.runs_folder or pdir
            print(f"error: no resumable run in {root / 'runs'}", file=sys.stderr)
            return 1

    return run_workflow(yaml_path, cli_vars=cli_vars, start_override=ns.node,
                        resume_dir=resume_dir, print_run_dir=ns.print_run_dir,
                        runs_root=ns.runs_folder)


def _print_version() -> None:
    try:
        from importlib.metadata import version
        v = version("medulla")
    except Exception:
        v = "source"
    commit = ""
    for stamp in (Path.home() / ".medulla" / "engine" / "INSTALLED_COMMIT",
                  Path.home() / ".medulla-engine" / "INSTALLED_COMMIT"):  # pre-4.0.4
        if stamp.is_file():
            commit = f"  ({stamp.read_text(encoding='utf-8').strip()})"
            break
    print(f"medulla {v}{commit}")


def _print_plan(workflow) -> None:
    p = workflow
    print(f"workflow: {p.path}")
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
