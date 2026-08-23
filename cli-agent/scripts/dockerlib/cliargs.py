"""Reading the command line before the engine sees it.

Split out of docker.py under the project's 250-line rule ($MAX_LOC). Two kinds of flag
meet here and must not be confused: OURS, consumed and never passed on (--build,
--cwd-ro, --mount), and the engine's, which travel through untouched and are only read
in passing (--var, --var-file, -w). Mixing them up means a flag either vanishes or is
handed to something that has never heard of it.
"""
from __future__ import annotations

import sys
from pathlib import Path


def parse(args: list[str]) -> tuple[list[str], dict]:
    """Returns (args for the engine, what WE need to know)."""
    out: dict = {}
    build = "--build" in args
    if build:
        args = [a for a in args if a != "--build"]

    # --cwd-ro is OURS: the engine has no container to mount anything into, so it is
    # consumed here. --runs-folder is NOT — it travels on, because the engine is what
    # actually roots the history there.
    cwd_ro = "--cwd-ro" in args
    if cwd_ro:
        args = [a for a in args if a != "--cwd-ro"]
    runs_folder = None
    for j, a in enumerate(args):
        if a == "--runs-folder" and j + 1 < len(args):
            runs_folder = Path(args[j + 1]).expanduser().resolve()
            # Rewrite it to the ABSOLUTE path, deliberately, and mount the folder at
            # that same path inside (below). --print-run-dir hands its answer to a
            # caller standing on the HOST while the run happened in the container, so
            # a container-only path would be unusable there — this file already carries
            # scars from exactly that. One absolute string, valid in both namespaces.
            args[j + 1] = str(runs_folder)
            break
    if cwd_ro and runs_folder is None:
        print("error: --cwd-ro requires --runs-folder (a read-only workspace leaves the "
              "run nowhere to write)", file=sys.stderr)
        return 1
    if runs_folder is not None:
        runs_folder.mkdir(parents=True, exist_ok=True)

    # Extract --mount / --mount-rw; also peek --var for Dockerfile resolution
    extra_mounts = []  # list of (path, ro:bool)
    var_files: list[Path] = []          # --var-file sources, mounted at their own paths
    cli_vars: dict[str, str] = {}
    clean_args = []
    i = 0
    while i < len(args):
        if args[i] == "--mount" and i + 1 < len(args):
            extra_mounts.append((args[i + 1], True))
            i += 2
        elif args[i] == "--mount-rw" and i + 1 < len(args):
            extra_mounts.append((args[i + 1], False))
            i += 2
        elif args[i] == "--var-file" and i + 1 < len(args):
            # The file has to exist INSIDE too, and its host path is what the engine
            # will open. Mount it at that same absolute path (the trick --runs-folder
            # already uses) and hand the absolute form on, so a relative one — or a
            # file outside the workspace — still resolves in there.
            key, _, raw = args[i + 1].partition("=")
            src = Path(raw).expanduser()
            if raw and not src.is_file():
                print(f"[docker.py] --var-file {key}: no such file: {src}", file=sys.stderr)
                return 1
            src = src.resolve()
            var_files.append(src)
            clean_args.append(args[i])
            clean_args.append(f"{key}={src}")
            i += 2
        elif args[i] == "--var" and i + 1 < len(args):
            kv = args[i + 1]
            if "=" in kv:
                k, v = kv.split("=", 1)
                cli_vars[k] = v
            clean_args.append(args[i])
            clean_args.append(args[i + 1])
            i += 2
        else:
            clean_args.append(args[i])
            i += 1
    args = clean_args

    # Extract workflow for Dockerfile resolution
    workflow = None
    for j, a in enumerate(args):
        if a in ("-w", "--workflow", "--workflow") and j + 1 < len(args):
            workflow = args[j + 1]
            break

    out.update(build=build, cwd_ro=cwd_ro, runs_folder=runs_folder,
               extra_mounts=extra_mounts, var_files=var_files, cli_vars=cli_vars,
               workflow=workflow)
    return args, out
