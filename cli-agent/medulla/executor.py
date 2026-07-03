import datetime
import os
import shutil
import signal
import subprocess
import sys
import threading
from pathlib import Path

from .output import eprint, write_log_line, write_prompt_file, log, ansi, ANSI_RE, BOLD, RED, YELLOW
from .signals import SIGNAL_RE, parse_var_attr, extract_text_from_json


def run_command(
    cmd: list[str],
    cwd: Path,
    timeout_sec: int,
    signal_cb=None,
    executor_hint: str | None = None,
) -> tuple[str, int, bool]:
    write_log_line(
        f"[medulla] run_command start pid={os.getpid()} cwd={cwd} timeout={timeout_sec} "
        f"exec={executor_hint or 'shell'} cmd={' '.join(cmd)}"
    )
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        start_new_session=True,
        # Expose the resolved step timeout so shell stages (and the CLIs they
        # launch) can size their own internal timeouts to it — single source of
        # truth is the pipeline's `timeout:` field.
        env={**os.environ, "MEDULLA_TIMEOUT_S": str(timeout_sec)},
    )
    interrupted = {"count": 0}

    stdout_lines: list[str] = []
    signal_buf = ""
    max_buf = 32768
    stop_requested = threading.Event()
    is_json_output = executor_hint in ("claude-code", "codex")

    def _process_signal_text(text: str) -> None:
        nonlocal signal_buf
        signal_buf += text
        last_end = 0
        for m in SIGNAL_RE.finditer(signal_buf):
            sig_name = m.group(1)
            attrs = parse_var_attr(m.group(2) or "")
            body = (m.group(3) or "").strip()
            should_stop = bool(signal_cb(sig_name, attrs, body)) if signal_cb is not None else False
            if should_stop and not stop_requested.is_set():
                stop_requested.set()
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except Exception:
                    pass
            last_end = m.end()
        if last_end > 0:
            signal_buf = signal_buf[last_end:]
        elif len(signal_buf) > max_buf and "<signal:" not in signal_buf:
            signal_buf = ""

    def stream_stdout(pipe) -> None:
        try:
            for line in iter(pipe.readline, ""):
                stdout_lines.append(line)
                ts = datetime.datetime.now().strftime("%H:%M:%S")
                write_log_line(f"[{ts}] [stdout] {line.rstrip()}")
                if executor_hint == "shell":
                    clean = ANSI_RE.sub("", line.rstrip())
                    if clean and "<signal:" not in clean:
                        eprint(f"[{ts}] {clean}")
                if signal_cb is not None:
                    if is_json_output:
                        extracted = extract_text_from_json(line.rstrip())
                        if extracted:
                            _process_signal_text(extracted + "\n")
                    else:
                        _process_signal_text(line)
        finally:
            try:
                pipe.close()
            except Exception:
                pass

    def stream_stderr(pipe) -> None:
        try:
            for line in iter(pipe.readline, ""):
                clean = ANSI_RE.sub("", line.rstrip())
                ts = datetime.datetime.now().strftime("%H:%M:%S")
                write_log_line(f"[{ts}] [stderr] {clean}")
                if executor_hint == "shell" and clean:
                    eprint(f"[{ts}] [stderr] {clean}")
        finally:
            try:
                pipe.close()
            except Exception:
                pass

    t_out = threading.Thread(target=stream_stdout, args=(proc.stdout,), daemon=True)
    t_err = threading.Thread(target=stream_stderr, args=(proc.stderr,), daemon=True)
    t_out.start()
    t_err.start()

    in_main_thread = threading.current_thread() is threading.main_thread()
    prev_sigint = signal.getsignal(signal.SIGINT) if in_main_thread else None

    def handle_sigint(signum, frame):
        interrupted["count"] += 1
        try:
            pgid = os.getpgid(proc.pid)
        except Exception:
            pgid = -1
        try:
            msg = (
                f"\n[medulla] SIGINT run_command count={interrupted['count']} "
                f"pid={proc.pid} pgid={pgid} cmd={' '.join(cmd)}\n"
            )
            os.write(2, msg.encode("utf-8", errors="replace"))
        except Exception:
            pass
        try:
            sig = signal.SIGTERM if interrupted["count"] == 1 else signal.SIGKILL
            os.killpg(os.getpgid(proc.pid), sig)
        except Exception:
            pass
        raise KeyboardInterrupt

    if in_main_thread:
        signal.signal(signal.SIGINT, handle_sigint)

    try:
        proc.wait(timeout=max(timeout_sec, 1))
        write_log_line(
            f"[medulla] run_command exit pid={os.getpid()} child_pid={proc.pid} "
            f"rc={proc.returncode} interrupted={interrupted['count']}"
        )
        t_out.join()
        t_err.join()
        return "".join(stdout_lines), proc.returncode, False
    except subprocess.TimeoutExpired:
        write_log_line(
            f"[medulla] run_command timeout pid={os.getpid()} child_pid={proc.pid} "
            f"timeout={timeout_sec}"
        )
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception:
            pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                pass
            proc.wait()
        t_out.join()
        t_err.join()
        return "".join(stdout_lines), 124, True
    except KeyboardInterrupt:
        write_log_line(
            f"[medulla] run_command KeyboardInterrupt pid={os.getpid()} child_pid={proc.pid} "
            f"count={interrupted['count']}"
        )
        try:
            sig = signal.SIGKILL if interrupted["count"] >= 2 else signal.SIGTERM
            os.killpg(os.getpgid(proc.pid), sig)
        except Exception:
            pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                pass
            proc.wait()
        t_out.join(timeout=2)
        t_err.join(timeout=2)
        raise
    finally:
        write_log_line(
            f"[medulla] run_command restore_sigint pid={os.getpid()} child_pid={proc.pid}"
        )
        if in_main_thread:
            signal.signal(signal.SIGINT, prev_sigint)


def run_shell(command: str, cwd: Path, dry_run: bool, timeout_sec: int, signal_cb=None) -> tuple[str, int, bool]:
    if dry_run:
        return "", 0, False
    shell = os.environ.get("SHELL", "bash")
    return run_command([shell, "-lc", command], cwd, timeout_sec, signal_cb=signal_cb, executor_hint="shell")


def _ensure_opencode_permissions(cwd: Path, model: str | None = None, effort: str | None = None, timeout_ms: int = 3600000) -> None:
    """Write opencode.json with permission=allow if not already present.

    When `effort` is set and `model` is a provider/model id (e.g.
    "zai-coding-plan/glm-5.2"), bind reasoningEffort for that model so the
    headless `opencode run` invocation reasons at the requested depth.
    `timeout_ms` sets the provider request timeout (default 5m is too short).
    """
    cfg = cwd / "opencode.json"
    if cfg.exists():
        return
    import json
    data: dict = {"$schema": "https://opencode.ai/config.json", "permission": "allow"}
    if model and "/" in model:
        provider, model_id = model.split("/", 1)
        # provider-level timeout (default 5m kills long reasoning runs);
        # reasoningEffort per-model only when requested.
        pblock: dict = {"options": {"timeout": timeout_ms}}
        if effort:
            pblock["models"] = {model_id: {"options": {"reasoningEffort": effort}}}
        data["provider"] = {provider: pblock}
    cfg.write_text(json.dumps(data) + "\n", encoding="utf-8")


def run_agent(executor: str, model: str | None, prompt: str, cwd: Path, dry_run: bool, timeout_sec: int, signal_cb=None, effort: str | None = None) -> tuple[str, int, bool]:
    if dry_run:
        return "", 0, False
    prompt_file = write_prompt_file(prompt)
    prompt_ref = f"@{prompt_file}" if prompt_file else prompt
    # Each agent CLI has its own internal timeout (agy/opencode default to 5m and
    # silently kill long runs). Tie it to the pipeline step timeout + 5min slack
    # so medulla's own timeout always fires first and the CLI timeout is a net.
    inner_s = timeout_sec + 300
    inner_ms = inner_s * 1000
    if executor == "claude-code":
        os.environ["API_TIMEOUT_MS"] = str(inner_ms)
        cmd = ["claude", "--dangerously-skip-permissions", "--output-format", "stream-json", "--verbose"]
        if model:
            cmd += ["--model", model]
        if prompt_file:
            cmd += ["--append-system-prompt-file", str(prompt_file)]
        cmd += ["-p", "Execute."]
    elif executor == "codex":
        # Prefer the `cx` wrapper (refreshes the codex token via the broker)
        # when it is on PATH; fall back to plain `codex` otherwise.
        codex_bin = shutil.which("cx") or "codex"
        cmd = [codex_bin, "exec", "--json", "--skip-git-repo-check"]
        if model:
            cmd += ["-c", f'model="{model}"']
        cmd += [
            "-c", f"model_reasoning_effort={effort or 'xhigh'}",
            "-c", f"stream_idle_timeout_ms={inner_ms}",
            "--dangerously-bypass-approvals-and-sandbox",
        ]
        cmd += [f"Execute. {prompt_ref}"]
    elif executor == "opencode":
        # Ensure opencode config allows all permissions (needed inside Docker)
        _ensure_opencode_permissions(cwd, model, effort, inner_ms)
        cmd = ["opencode", "run", "--agent", "build"]
        if model:
            cmd += ["-m", model]
        cmd += ["Execute.", prompt_ref]
    elif executor == "gemini":
        # Gemini uses GEMINI_SYSTEM_MD env var to load system prompt from file
        if prompt_file:
            os.environ["GEMINI_SYSTEM_MD"] = str(prompt_file)
        cmd = ["gemini", "--approval-mode", "yolo"]
        if model:
            cmd += ["-m", model]
        cmd += ["-p", "Execute."]
    elif executor == "agy":
        # --print-timeout default is 5m and silently kills long runs; tie to step timeout.
        cmd = ["agy", "--dangerously-skip-permissions", "--print-timeout", f"{inner_s}s"]
        if model:
            cmd += ["--model", model]
        cmd += ["--print", f"Execute. {prompt_ref}"]
    else:
        cmd = [executor]
        if model:
            cmd += ["--model", model]
        cmd += ["-p", "Execute.", prompt_ref]
    write_log_line(f"exec: {' '.join(cmd)}")
    return run_command(cmd, cwd, timeout_sec, signal_cb=signal_cb, executor_hint=executor)


def runtime_diagnostics() -> None:
    tools = ["claude", "codex", "opencode", "agy", "gh", "rg", "python3"]
    for t in tools:
        path = shutil.which(t)
        if not path:
            eprint(f"runtime: tool {t} v- (missing)")
            continue
        version = None
        for args in (["--version"], ["-v"], ["version"]):
            try:
                proc = subprocess.run([t, *args], capture_output=True, text=True, check=False, timeout=3)
            except Exception:
                continue
            if proc.returncode != 0:
                continue
            out = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
            if out:
                version = out.splitlines()[0][:140]
                break
        if version:
            ver = version.strip()
            if ver and not ver.lower().startswith("v"):
                ver = f"v{ver}"
            eprint(f"runtime: tool {t} {ver}")
        else:
            eprint(f"runtime: tool {t} ok")

    bridge_shim = shutil.which("bridge-shim")
    if bridge_shim:
        cargo = shutil.which("cargo")
        active = "yes" if cargo and os.path.realpath(cargo) == os.path.realpath(bridge_shim) else "no"
        eprint(f"runtime: bridge_shim path={bridge_shim} cargo={active}")
    else:
        eprint(f"runtime: bridge_shim not-installed")

    bridge = Path(os.environ.get("MEDULLA_BRIDGE", "/tmp/medulla-bridge"))
    bridge_state = "mounted" if bridge.is_dir() else "not-mounted"
    eprint(f"runtime: host_bridge {bridge_state}")


def confirm_non_docker_max_permissions() -> int:
    eprint("")
    eprint(ansi("!!! RUNNING WITH MAX PERMISSIONS !!!", BOLD + RED))
    eprint(ansi("Press ENTER to continue", BOLD + YELLOW))
    eprint("")
    if not sys.stdin.isatty():
        eprint("warning: stdin is not a TTY, continue without confirmation")
        return 0
    try:
        input()
    except KeyboardInterrupt:
        eprint("cancelled by user")
        return 130
    return 0
