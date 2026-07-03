import datetime
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from .output import (
    eprint, log, ansi, round_banner, round_stats, format_signal_line,
    set_round_log_file, write_log_line, write_metric, write_raw_output,
    _safe_write_stderr, _safe_write_stdout,
    BOLD, GREEN, YELLOW, RED,
)
from .state import load_vars, save_var, delete_var, export_vars, clear_vars
from .pipeline import EXIT_STAGE, load_pipeline, validate_pipeline, render_text
from .signals import extract_signals, extract_text_from_json, parse_var_attr
from .executor import run_shell, run_agent


NEXT_ITEM = "__next_item__"


def _task_id() -> str:
    return os.environ.get("MEDULLA_TASK_ID", "").strip()


def _bridge_dir() -> Path:
    return Path(os.environ.get("MEDULLA_BRIDGE", "/tmp/medulla-bridge"))


def _emulator_pid_file() -> Path:
    tid = _task_id()
    name = f"emulator.{tid}.pid" if tid else "emulator.pid"
    return Path.cwd() / ".medulla" / name


def _parse_llm(llm_val: str, model_override: str | None = None) -> tuple[str, str | None]:
    """Parse 'executor:model' or plain 'executor' string."""
    if ":" in llm_val:
        executor, model = llm_val.split(":", 1)
    else:
        executor, model = llm_val, None
    if model_override:
        model = model_override
    return executor, model


def _parse_runner(runner: dict) -> tuple[str | None, str | None, str | None, str | None]:
    """Parse runner block to (executor, model, effort, command).

    New format:  shell: <cmd>  or  llm: <name>[:<model>] + optional model: <m>
                 + optional effort: <high|max|xhigh|...> (reasoning depth)
    Legacy:      executor: <name>, model: <m>, command: <cmd>
    """
    if "loop" in runner:
        return "loop", None, None, None
    if "shell" in runner:
        return "shell", None, None, runner["shell"]
    effort = runner.get("effort")
    if "llm" in runner:
        executor, model = _parse_llm(str(runner["llm"]), runner.get("model"))
        return executor, model, effort, None
    # legacy executor/model/command format
    return runner.get("executor"), runner.get("model"), effort, runner.get("command")


def _resolve_signal_target(target):
    """Resolve on_signal value to (stage, steps, reset_iterations).

    String  -> simple transition
    Dict    -> transition with options (reset_iterations)
    Array   -> steps + transition
    """
    if isinstance(target, str):
        return target, [], False
    if isinstance(target, dict):
        return target["stage"], [], bool(target.get("reset_iterations", False))
    if isinstance(target, list):
        steps = []
        stage_name = EXIT_STAGE
        reset_iters = False
        for item in target:
            if "stage" in item:
                stage_name = item["stage"]
            elif item.get("reset_iterations"):
                reset_iters = True
            else:
                steps.append(item)
        return stage_name, steps, reset_iters
    return EXIT_STAGE, [], False


def _execute_steps(steps, workdir, pipeline_dir, vars_map, dry_run):
    """Execute signal hook steps (runner blocks)."""
    for step in steps:
        runner = step["runner"]
        if "shell" in runner:
            cmd = render_text(str(runner["shell"]), pipeline_dir, vars_map)
            hook_out, _, _ = run_shell(cmd, workdir, dry_run, int(runner.get("timeout", 60)))
            for s_name, s_attrs, s_body in extract_signals(hook_out):
                if s_name == "update":
                    eprint(format_signal_line(s_name, s_attrs, s_body))
        elif "llm" in runner:
            executor, model = _parse_llm(str(runner["llm"]), runner.get("model"))
            prompt = render_text(str(runner.get("prompt", "")), pipeline_dir, vars_map)
            timeout = int(runner.get("timeout", 60))
            output, _, _ = run_agent(executor, model, prompt, workdir, dry_run, timeout, effort=runner.get("effort"))
            for s_name, s_attrs, s_body in extract_signals(output):
                if s_name == "update":
                    eprint(format_signal_line(s_name, s_attrs, s_body))


def _run_loop_parallel(
    loop_config: dict,
    stage_on_signal: dict,
    runner_cfg: dict,
    items: list,
    pipeline_dir,
    workdir,
    vars_map: dict,
    default_timeout_sec: int,
    dry_run: bool,
) -> tuple[str, list]:
    """Run all loop items concurrently. Returns (overall_signal, results).

    overall_signal is "loop_done" when all items succeed (signal → __next_item__),
    or "failed" when any item fails without emitting a success signal.
    """
    import concurrent.futures
    import threading

    lock = threading.Lock()

    # Signals that advance the loop (map to __next_item__)
    next_item_signals = {
        sig for sig, target in stage_on_signal.items()
        if _resolve_signal_target(target)[0] == NEXT_ITEM
    }
    ignore_rc = bool(runner_cfg.get("ignore_rc", False))

    def run_item(item: str):
        item_vars = dict(vars_map)
        item_vars["__list_item__"] = item
        item_vars["__item__"] = item

        fetch_cmd = loop_config.get("fetch")
        if fetch_cmd:
            rendered_fetch = render_text(str(fetch_cmd), pipeline_dir, item_vars)
            fetch_output, _, _ = run_shell(rendered_fetch, workdir, False, 30)
            item_vars["__item__"] = fetch_output.strip()

        ex, mdl, eff, cmd = _parse_runner(runner_cfg)
        timeout_sec = int(runner_cfg.get("timeout", default_timeout_sec))

        if ex == "shell":
            rendered_cmd = render_text(str(cmd or ""), pipeline_dir, item_vars)
            output, rc, timed_out = run_shell(rendered_cmd, workdir, dry_run, timeout_sec)
        else:
            prompt = render_text(str(runner_cfg.get("prompt", "")), pipeline_dir, item_vars)
            output, rc, timed_out = run_agent(ex, mdl, prompt, workdir, dry_run, timeout_sec, effort=eff)

        if ex in ("claude-code", "codex"):
            filtered = [
                extracted
                for line in output.splitlines()
                if (extracted := extract_text_from_json(line.rstrip()))
            ]
            signals = extract_signals("\n".join(filtered))
        else:
            signals = extract_signals(output)

        transition = "default"
        for sig_name, _, _ in signals:
            if sig_name in stage_on_signal:
                transition = sig_name
                break

        with lock:
            ts = datetime.datetime.now().strftime("%H:%M:%S")
            status = ansi(f"[{ts}] parallel item={item} signal={transition} rc={rc}", BOLD)
            _safe_write_stderr(status)
            log(f"parallel item={item} signal={transition} rc={rc} timed_out={timed_out}")

        return item, transition, rc, timed_out, signals

    raw_cap = loop_config.get("max_parallel")
    try:
        cap = int(raw_cap) if raw_cap else len(items)
    except (TypeError, ValueError):
        cap = len(items)
    workers = max(1, min(len(items), cap))
    cap_note = f" (max {workers} concurrent)" if workers < len(items) else ""
    eprint(ansi(f"loop: running {len(items)} items in parallel{cap_note}", BOLD + GREEN))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(run_item, item) for item in items]
        results = [f.result() for f in concurrent.futures.as_completed(futures)]

    failed = [
        r for r in results
        if r[1] not in next_item_signals and (r[2] != 0 or r[1] == "failed") and not ignore_rc
    ]
    if failed:
        return "failed", results
    return "loop_done", results


def _cleanup_bridge_bg():
    """Kill any background processes started via bridge."""
    vars_map = load_vars()

    host_builder_pid_file = vars_map.get("HOST_BUILDER_PID_FILE", "")
    dev_pid_file = vars_map.get("DEV_PID_FILE", "")
    dev_pgid_file = vars_map.get("DEV_PGID_FILE", "")
    dev_container_file = vars_map.get("DEV_CONTAINER_FILE", "")
    analyze_chunk = vars_map.get("ANALYZE_CHUNK", "")
    target_cursor_file = vars_map.get("TARGET_CURSOR_FILE", "")
    target_dir = vars_map.get("TARGET_DIR", "")

    def _read_text(path_str: str) -> str:
        if not path_str:
            return ""
        path = Path(path_str)
        if not path.is_file():
            return ""
        try:
            return path.read_text().strip()
        except OSError:
            return ""

    def _unlink(path_str: str) -> None:
        if path_str:
            Path(path_str).unlink(missing_ok=True)

    def _kill_pidfile(path_str: str) -> None:
        pid_text = _read_text(path_str)
        if pid_text:
            try:
                pid = int(pid_text)
                os.kill(pid, signal.SIGTERM)
                log(f"cleanup: killed pid={pid} from {path_str}")
            except (ValueError, ProcessLookupError, OSError):
                pass
        _unlink(path_str)

    def _kill_pgidfile(path_str: str) -> None:
        pgid_text = _read_text(path_str)
        if pgid_text:
            try:
                pgid = int(pgid_text)
                os.killpg(pgid, signal.SIGTERM)
                log(f"cleanup: killed pgid={pgid} from {path_str}")
            except (ValueError, ProcessLookupError, OSError):
                pass
        _unlink(path_str)

    def _stop_container(path_str: str) -> None:
        cid = _read_text(path_str)
        if cid:
            try:
                subprocess.run(["docker", "stop", "-t", "2", cid], capture_output=True, timeout=10)
                log(f"cleanup: stopped docker container {cid} from {path_str}")
            except Exception:
                pass
        _unlink(path_str)

    _kill_pgidfile(dev_pgid_file)
    _kill_pidfile(dev_pid_file)
    _stop_container(dev_container_file)

    pid_file = _emulator_pid_file()
    bridge = _bridge_dir()
    if pid_file.is_file() and bridge.is_dir():
        pid = pid_file.read_text().strip()
        if pid:
            log(f"cleanup: killing bridge background pid={pid}")
            (bridge / "request").write_text(f"kill:{pid}")
            import time
            time.sleep(1)
            for f in ("response", "exit_code"):
                    (bridge / f).unlink(missing_ok=True)
        pid_file.unlink(missing_ok=True)

    _kill_pidfile(host_builder_pid_file)
    if target_dir:
        tid = _task_id()
        ename = f"emulator.{tid}.pid" if tid else "emulator.pid"
        (Path(target_dir) / ".medulla" / ename).unlink(missing_ok=True)
    _unlink(analyze_chunk)
    _unlink(target_cursor_file)
    bridge = _bridge_dir()
    if bridge.is_dir():
        try:
            for child in bridge.iterdir():
                child.unlink(missing_ok=True)
            bridge.rmdir()
        except OSError:
            pass


def run_pipeline(path: Path, dry_run: bool = False, verbose: bool = False, cli_vars: dict[str, str] | None = None, start_stage: str | None = None) -> int:
    data = load_pipeline(path)
    validate_pipeline(data)

    default_timeout_sec = int(data.get("round_timeout", 3600))
    pipeline_fallback = data.get("fallback_runner")

    if dry_run:
        stages = data["stages"]
        print(f"Pipeline: {path}")
        print(f"Version: {data.get('version', 'unknown')}")
        print(f"Start: {data['starting']}")
        print("\nStages:")
        for name, stage in stages.items():
            runner = stage.get("runner", {})
            loop = runner.get("loop") if isinstance(runner, dict) else None
            display_runner = loop.get("runner", runner) if loop else runner
            executor, model, _, _ = _parse_runner(display_runner)
            engine = f"{executor}/{model}" if model else executor
            loop_info = f" [loop: {loop['list']}]" if loop else ""
            print(f"- {name}: {engine}{loop_info}")
            on_signal = stage.get("on_signal", {})
            for sig, target in on_signal.items():
                stage_target, _, _ = _resolve_signal_target(target)
                print(f"    {sig} -> {stage_target}")
        return 0

    pipeline_dir = path.parent
    workdir = Path.cwd()
    vars_map = {k: str(v) for k, v in (data.get("vars") or {}).items()}
    # Seed MEDULLA_TASK_ID into vars so metrics, resume, and templates see it.
    # The id is resolved + published to env by cli.py at startup; persist it so
    # an interrupted run resumes against the same isolated state file.
    tid = os.environ.get("MEDULLA_TASK_ID", "").strip()
    if tid:
        vars_map["MEDULLA_TASK_ID"] = tid
        save_var("MEDULLA_TASK_ID", tid)
    disk_vars = load_vars()
    vars_map.update(disk_vars)
    if cli_vars:
        vars_map.update(cli_vars)
        for k, v in cli_vars.items():
            save_var(k, v)
    stages = data["stages"]
    stage_name = data["starting"]

    if start_stage:
        if start_stage in stages:
            eprint(ansi(f"--stage: skipping to '{start_stage}'", YELLOW))
            stage_name = start_stage
        else:
            eprint(ansi(f"error: stage '{start_stage}' not found in pipeline", RED))
            return 1
    elif (saved := load_vars().get("_loop_stage")) and saved in stages:
        eprint(ansi(f"resume: resuming from saved state '{saved}'", YELLOW))
        stage_name = saved
    else:
        if saved:
            eprint(ansi(f"warn: saved state '{saved}' not found in stages, starting from '{stage_name}'", YELLOW))
        for key in list(vars_map):
            if key.startswith("_iter_"):
                vars_map.pop(key)
                delete_var(key)
        delete_var("_loop_stage")

    log(f"run_pipeline: start={stage_name} workdir={workdir} pipeline_dir={pipeline_dir}")

    import atexit
    import signal as _sig
    atexit.register(_cleanup_bridge_bg)

    def _sigterm_handler(signum, frame):
        _cleanup_bridge_bg()
        raise SystemExit(128 + signum)

    _sig.signal(_sig.SIGTERM, _sigterm_handler)
    _sig.signal(_sig.SIGINT, _sigterm_handler)

    rounds = 0
    max_rounds = 500
    total_round_time = 0.0
    loop_state: dict[str, dict] = {}  # stage_name -> {items, index}
    while stage_name != EXIT_STAGE and rounds < max_rounds:
        rounds += 1
        # Reload vars from disk every round to prevent in-memory drift
        disk_vars = load_vars()
        for k, v in disk_vars.items():
            if k not in vars_map or not vars_map[k]:
                vars_map[k] = v
                log(f"var restored from disk: {k}={v}")
        iter_key = f"_iter_{stage_name}"
        iter_count = int(vars_map.get(iter_key, "0"))
        max_iterations = stages[stage_name].get("max_iterations")
        set_round_log_file(rounds, stage_name)
        export_vars()
        started = time.monotonic()
        stage = stages[stage_name]
        stage_on_signal = stage["on_signal"]
        runner = stage["runner"]
        loop_config = runner.get("loop") if isinstance(runner, dict) else None
        if loop_config:
            runner = loop_config["runner"]  # swap to inner runner
        executor, model, effort, command = _parse_runner(runner)
        output = ""
        rc = 0
        timed_out = False
        realtime_signals: list[tuple[str, dict[str, str], str]] = []
        realtime_transition_signal = "default"
        known_signals = (set(stage_on_signal.keys()) - {"on_max"}) | {"update", "var"}

        def on_realtime_signal(sig_name: str, attrs: dict[str, str], body: str) -> bool:
            nonlocal realtime_transition_signal
            if sig_name not in known_signals:
                return False
            is_duplicate = (sig_name, attrs, body) in realtime_signals
            realtime_signals.append((sig_name, attrs, body))
            if is_duplicate:
                return realtime_transition_signal != "default"
            ts = datetime.datetime.now().strftime("%H:%M:%S")
            raw = f"<signal:{sig_name}>{body}</signal:{sig_name}>"
            write_log_line(f"[{ts}] {raw}")
            _safe_write_stderr(f"[{ts}] {format_signal_line(sig_name, attrs, body)}")
            if sig_name == "var":
                key = attrs.get("key")
                if key and body.strip():
                    vars_map[key] = body
                    save_var(key, body)
                    log(f"set var (realtime): {key}={body}")
                elif key:
                    log(f"skip realtime var with empty body: {key} (existing={vars_map.get(key, '')})")
                return False
            if sig_name == "update":
                return False
            if sig_name in stage_on_signal:
                realtime_transition_signal = sig_name
                log(f"transition signal (realtime): {sig_name}")
                return True
            return False

        log(f"round={rounds} stage={stage_name} executor={executor}")
        eprint("")

        # ── Guard: skip stage if prompt needs __list_item__ but it's empty ──
        if not loop_config and not vars_map.get("__list_item__"):
            prompt_tmpl = str(runner.get("prompt", ""))
            if "{{__list_item__}}" in prompt_tmpl or "{{__item__}}" in prompt_tmpl:
                fallback_signal = "ready" if "ready" in stage_on_signal else next(iter(stage_on_signal), "default")
                raw_fb = stage_on_signal.get(fallback_signal, EXIT_STAGE)
                fb_stage, _, _ = _resolve_signal_target(raw_fb)
                eprint(ansi(
                    f"warn: stage '{stage_name}' requires __list_item__ but it is empty, "
                    f"skipping -> '{fb_stage}'",
                    YELLOW,
                ))
                log(f"skip_empty_item stage={stage_name} -> {fb_stage}")
                stage_name = fb_stage
                save_var("_loop_stage", stage_name)
                continue

        if max_iterations and iter_count >= max_iterations:
            eprint(round_banner(rounds, stage_name, executor or "unknown", model, iter_count + 1))
            finally_runner = stage.get("finally")
            if finally_runner:
                log(f"finally: executing for stage '{stage_name}' (on_max)")
                try:
                    _execute_steps([{"runner": finally_runner}], workdir, pipeline_dir, vars_map, False)
                except Exception as exc:
                    log(f"finally: error in stage '{stage_name}': {exc}")
            on_max_target = stage_on_signal.get("on_max")
            if on_max_target:
                next_s, steps, _ = _resolve_signal_target(on_max_target)
                if next_s in stages or next_s == EXIT_STAGE:
                    eprint(ansi(f"max_iterations: '{stage_name}' reached {max_iterations} -> '{next_s}'", YELLOW))
                    _execute_steps(steps, workdir, pipeline_dir, vars_map, False)
                    vars_map.pop(iter_key, None)
                    delete_var(iter_key)
                    stage_name = next_s
                    save_var("_loop_stage", stage_name)
                    continue
            eprint(ansi(f"error: stage '{stage_name}' exceeded max_iterations={max_iterations}, no on_max handler, stopping", RED))
            return 1

        # ── Loop initialization ──
        if loop_config and stage_name not in loop_state:
            list_cmd = render_text(str(loop_config["list"]), pipeline_dir, vars_map)
            log(f"loop: running list command: {list_cmd}")
            list_output, list_rc, _ = run_shell(list_cmd, workdir, False, 30)
            items = [line.strip() for line in list_output.strip().splitlines() if line.strip()]
            if not items:
                eprint(ansi(f"loop: command returned 0 items — skipping", YELLOW))
                log("loop: 0 items, firing loop_done")
                raw_done = stage_on_signal.get("loop_done", EXIT_STAGE)
                done_stage, done_steps, _ = _resolve_signal_target(raw_done)
                _execute_steps(done_steps, workdir, pipeline_dir, vars_map, False)
                stage_name = done_stage
                save_var("_loop_stage", stage_name)
                continue
            loop_state[stage_name] = {"items": items, "index": 0}

        # ── Parallel loop shortcut ──
        if loop_config and loop_config.get("parallel") and stage_name in loop_state:
            ls = loop_state[stage_name]
            eprint(round_banner(rounds, stage_name, executor or "unknown", model, iter_count + 1))
            overall_signal, par_results = _run_loop_parallel(
                loop_config, stage_on_signal, runner,
                ls["items"], pipeline_dir, workdir, vars_map,
                default_timeout_sec, dry_run,
            )
            del loop_state[stage_name]
            vars_map.pop("__item__", None)
            vars_map.pop("__list_item__", None)
            iter_count += 1
            vars_map[iter_key] = str(iter_count)
            save_var(iter_key, str(iter_count))
            duration = time.monotonic() - started
            total_round_time += duration
            avg_duration = total_round_time / rounds
            if overall_signal == "loop_done":
                raw_done = stage_on_signal.get("loop_done", EXIT_STAGE)
                done_stage, done_steps, _ = _resolve_signal_target(raw_done)
                _execute_steps(done_steps, workdir, pipeline_dir, vars_map, False)
                eprint(ansi(f"loop: {len(ls['items'])}/{len(ls['items'])} items completed in parallel", BOLD + GREEN))
                eprint(round_stats(done_stage, duration, avg_duration))
                write_metric({
                    "ts": datetime.datetime.now().isoformat(),
                    "round": rounds, "stage": stage_name, "executor": executor,
                    "model": model, "duration_s": round(duration, 2),
                    "signal": "loop_done", "next": done_stage, "rc": 0, "timed_out": False,
                    "task_id": vars_map.get("MEDULLA_TASK_ID", ""),
                })
                stage_name = done_stage
            else:
                raw_target = stage_on_signal.get(overall_signal, stage_on_signal.get("default", EXIT_STAGE))
                next_s, steps, _ = _resolve_signal_target(raw_target)
                _execute_steps(steps, workdir, pipeline_dir, vars_map, False)
                eprint(round_stats(next_s, duration, avg_duration))
                write_metric({
                    "ts": datetime.datetime.now().isoformat(),
                    "round": rounds, "stage": stage_name, "executor": executor,
                    "model": model, "duration_s": round(duration, 2),
                    "signal": overall_signal, "next": next_s, "rc": 1, "timed_out": False,
                    "task_id": vars_map.get("MEDULLA_TASK_ID", ""),
                })
                stage_name = next_s
            save_var("_loop_stage", stage_name)
            continue

        # ── Loop per-iteration setup ──
        if stage_name in loop_state:
            ls = loop_state[stage_name]
            current_item = ls["items"][ls["index"]]
            vars_map["__list_item__"] = current_item
            vars_map["__item__"] = current_item
            fetch_cmd = loop_config.get("fetch") if loop_config else None
            if fetch_cmd:
                fetch_cmd = render_text(str(fetch_cmd), pipeline_dir, vars_map)
                log(f"loop: running fetch command: {fetch_cmd}")
                fetch_output, _, _ = run_shell(fetch_cmd, workdir, False, 30)
                vars_map["__item__"] = fetch_output.strip()

        # ── Round banner (after loop init so we have item info) ──
        loop_kw = {}
        if stage_name in loop_state:
            ls = loop_state[stage_name]
            loop_kw = {
                "loop_index": ls["index"] + 1,
                "loop_total": len(ls["items"]),
                "loop_item": ls["items"][ls["index"]],
            }
        eprint(round_banner(rounds, stage_name, executor or "unknown", model, iter_count + 1, **loop_kw))
        task_id = vars_map.get("MEDULLA_TASK_ID", "")
        if task_id:
            log(f"MEDULLA_TASK_ID={task_id} (round {rounds})")
        else:
            log(f"warn: MEDULLA_TASK_ID is empty at round {rounds} stage={stage_name}")

        if executor == "shell":
            command = render_text(str(command or ""), pipeline_dir, vars_map)
            log(f"shell command length={len(command)} chars")
            timeout_sec = int(runner.get("timeout", default_timeout_sec))
            log(
                f"stage_exec kind=shell stage={stage_name} round={rounds} "
                f"timeout={timeout_sec} pid={os.getpid()}"
            )
            try:
                output, rc, timed_out = run_shell(command, workdir, dry_run, timeout_sec, signal_cb=on_realtime_signal)
            except KeyboardInterrupt:
                eprint(
                    f"[medulla] stage_interrupt kind=shell stage={stage_name} round={rounds} pid={os.getpid()}"
                )
                raise
        else:
            max_attempts = 2
            attempts_list = [(executor, model, effort, runner)]
            fallback = stage.get("fallback_runner") or pipeline_fallback
            if isinstance(fallback, dict):
                fb_exec, fb_model, fb_effort, _ = _parse_runner(fallback)
                attempts_list.append((fb_exec, fb_model, fb_effort, fallback))

            primary_prompt = str(runner.get("prompt", ""))
            output, rc, timed_out = "", 1, False
            for run_executor, run_model, run_effort, run_runner in attempts_list:
                prompt_tmpl = str(run_runner.get("prompt", "")) or primary_prompt
                prompt = render_text(prompt_tmpl, pipeline_dir, vars_map)
                for attempt in range(1, max_attempts + 1):
                    log(f"agent executor={run_executor} model={run_model} prompt_length={len(prompt)} attempt={attempt}/{max_attempts}")
                    timeout_sec = int(run_runner.get("timeout", default_timeout_sec))
                    if run_executor is None:
                        raise ValueError(f"runner for stage '{stage_name}' is missing llm/executor")
                    log(
                        f"stage_exec kind=agent stage={stage_name} round={rounds} "
                        f"executor={run_executor} model={run_model} attempt={attempt}/{max_attempts} "
                        f"timeout={timeout_sec} pid={os.getpid()}"
                    )
                    try:
                        output, rc, timed_out = run_agent(
                            run_executor,
                            run_model,
                            prompt,
                            workdir,
                            dry_run,
                            timeout_sec,
                            signal_cb=on_realtime_signal,
                            effort=run_effort,
                        )
                    except KeyboardInterrupt:
                        eprint(
                            f"[medulla] stage_interrupt kind=agent stage={stage_name} round={rounds} "
                            f"executor={run_executor} model={run_model} attempt={attempt}/{max_attempts} pid={os.getpid()}"
                        )
                        raise
                    if rc == 0 or realtime_transition_signal != "default":
                        break
                    eprint(ansi(f"warn: executor failed (attempt {attempt}/{max_attempts}, exit {rc})", YELLOW))
                if rc == 0 or realtime_transition_signal != "default":
                    break
                if fallback and run_runner is not fallback:
                    fb_name = fallback.get("llm") or fallback.get("executor", "?")
                    eprint(ansi(f"warn: switching to fallback executor '{fb_name}'", YELLOW))

        log(f"output length={len(output)} chars")
        write_raw_output(output)

        # For JSON-output executors, filter output through extract_text_from_json
        # to avoid false signals from echoed prompt files in command_execution output.
        if executor in ("claude-code", "codex"):
            filtered_parts = []
            for line in output.splitlines():
                extracted = extract_text_from_json(line.rstrip())
                if extracted:
                    filtered_parts.append(extracted)
            signals = extract_signals("\n".join(filtered_parts))
        else:
            signals = extract_signals(output)
        transition_signal = realtime_transition_signal

        log(f"signals found: {[s[0] for s in signals]}")
        for sig_name, attrs, body in signals:
            if sig_name not in known_signals:
                continue
            signal_line = format_signal_line(sig_name, attrs, body)
            if sig_name == "var":
                key = attrs.get("key")
                if key and body:
                    # Skip if already set via realtime and new body is empty or same
                    existing = vars_map.get(key)
                    if existing and not body.strip():
                        log(f"skip empty var overwrite: {key} (keeping={existing})")
                    else:
                        vars_map[key] = body
                        save_var(key, body)
                        log(f"set var: {key}={body}")
                        if (sig_name, attrs, body) not in realtime_signals:
                            eprint(signal_line)
                elif key and not body:
                    log(f"skip var with empty body: {key} (existing={vars_map.get(key, '')})")
                continue

            if sig_name == "update":
                if (sig_name, attrs, body) not in realtime_signals:
                    eprint(signal_line)
                continue

            if sig_name in stage_on_signal:
                if transition_signal == "default":
                    transition_signal = sig_name
                    if (sig_name, attrs, body) not in realtime_signals:
                        eprint(signal_line)
                    log(f"transition signal: {transition_signal}")
                break

            if (sig_name, attrs, body) not in realtime_signals:
                eprint(signal_line)

        if verbose and output.strip():
            _safe_write_stdout(output)

        # Increment iteration counter on stage exit
        iter_count += 1
        vars_map[iter_key] = str(iter_count)
        save_var(iter_key, str(iter_count))

        # ── Stage finally block ──
        finally_runner = stage.get("finally")
        if finally_runner:
            log(f"finally: executing for stage '{stage_name}'")
            try:
                _execute_steps([{"runner": finally_runner}], workdir, pipeline_dir, vars_map, dry_run)
            except Exception as exc:
                log(f"finally: error in stage '{stage_name}': {exc}")

        has_default = "default" in stage_on_signal
        raw_target = stage_on_signal.get(transition_signal, stage_on_signal.get("default", EXIT_STAGE))
        next_stage, steps, reset_iters = _resolve_signal_target(raw_target)

        _execute_steps(steps, workdir, pipeline_dir, vars_map, dry_run)
        if reset_iters:
            iter_key = f"_iter_{stage_name}"
            vars_map.pop(iter_key, None)
            delete_var(iter_key)

        # ── Loop advancement ──
        if next_stage == NEXT_ITEM:
            if stage_name not in loop_state:
                eprint(ansi(f"error: __next__ signal but no active loop on stage '{stage_name}'", RED))
                return 1
            ls = loop_state[stage_name]
            ls["index"] += 1
            duration = time.monotonic() - started
            total_round_time += duration
            avg_duration = total_round_time / rounds
            if ls["index"] < len(ls["items"]):
                eprint(round_stats(stage_name, duration, avg_duration))
                write_metric({
                    "ts": datetime.datetime.now().isoformat(),
                    "round": rounds, "stage": stage_name, "executor": executor,
                    "model": model, "duration_s": round(duration, 2),
                    "signal": transition_signal, "next": stage_name,
                    "rc": rc, "timed_out": timed_out,
                    "task_id": vars_map.get("MEDULLA_TASK_ID", ""),
                    "loop_item": ls["items"][ls["index"] - 1],
                })
                save_var("_loop_stage", stage_name)
                continue
            else:
                total = len(ls["items"])
                eprint(ansi(f"loop: {total}/{total} iterations completed", BOLD + GREEN))
                del loop_state[stage_name]
                vars_map.pop("__item__", None)
                vars_map.pop("__list_item__", None)
                raw_done = stage_on_signal.get("loop_done", EXIT_STAGE)
                done_stage, done_steps, _ = _resolve_signal_target(raw_done)
                _execute_steps(done_steps, workdir, pipeline_dir, vars_map, False)
                eprint(round_stats(done_stage, duration, avg_duration))
                write_metric({
                    "ts": datetime.datetime.now().isoformat(),
                    "round": rounds, "stage": stage_name, "executor": executor,
                    "model": model, "duration_s": round(duration, 2),
                    "signal": "loop_done", "next": done_stage,
                    "rc": rc, "timed_out": timed_out,
                    "task_id": vars_map.get("MEDULLA_TASK_ID", ""),
                })
                stage_name = done_stage
                save_var("_loop_stage", stage_name)
                continue

        # Clean up loop state if leaving a looped stage via non-loop signal
        if stage_name in loop_state:
            # Persist current item as a stage-scoped VAR so downstream non-loop stages can access it
            cur_item = vars_map.get("__list_item__", "")
            if cur_item:
                item_var = f"_{stage_name}_loop_item"
                vars_map[item_var] = cur_item
                save_var(item_var, cur_item)
                log(f"loop: saved {item_var}={cur_item}")
            log(f"loop: leaving stage '{stage_name}' via signal '{transition_signal}', cleaning up")
            del loop_state[stage_name]
            vars_map.pop("__item__", None)
            vars_map.pop("__list_item__", None)

        ignore_rc = bool(runner.get("ignore_rc", False))
        if rc != 0 and transition_signal == "default" and not ignore_rc:
            eprint(ansi(f"error: stage '{stage_name}' failed with exit code {rc}", RED))
            if timed_out:
                eprint(ansi(f"error: stage '{stage_name}' timed out", RED))
            return rc
        if rc != 0 and (transition_signal != "default" or ignore_rc):
            eprint(ansi(f"warn: stage '{stage_name}' rc={rc} ignored due to signal transition", YELLOW))

        duration = time.monotonic() - started
        total_round_time += duration
        avg_duration = total_round_time / rounds
        eprint(round_stats(next_stage, duration, avg_duration))
        write_metric({
            "ts": datetime.datetime.now().isoformat(),
            "round": rounds,
            "stage": stage_name,
            "executor": executor,
            "model": model,
            "duration_s": round(duration, 2),
            "signal": transition_signal,
            "next": next_stage,
            "rc": rc,
            "timed_out": timed_out,
            "task_id": vars_map.get("MEDULLA_TASK_ID", ""),
        })
        if transition_signal == "default" and not has_default:
            eprint(ansi(f"error: stage '{stage_name}' produced no transition signal and has no default", RED))
            if output.strip():
                eprint(output.strip())
            return 1
        log(f"next_stage={next_stage} (from signal={transition_signal})")
        if next_stage == EXIT_STAGE:
            clear_vars()
            if transition_signal == "failed":
                eprint(ansi(f"finish: exit on signal '{transition_signal}' from stage '{stage_name}'", RED))
                return 2
            if transition_signal == "default" and not has_default:
                eprint(ansi(
                    f"finish: exit via default signal from stage '{stage_name}' "
                    f"(no explicit signal emitted)",
                    RED,
                ))
                return 3
            eprint("")
            eprint(ansi(f"Pipeline finished successfully ({rounds} rounds, {total_round_time:.0f}s total)", BOLD + GREEN))
            return 0
        stage_name = next_stage
        save_var("_loop_stage", stage_name)

    if rounds >= max_rounds:
        eprint("error: max rounds exceeded")
        return 1
    clear_vars()
    return 0
