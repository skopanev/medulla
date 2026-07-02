import datetime
import json
import os
import re
import select
import sys
import threading
import time
from pathlib import Path

VERBOSE = False
LOG_DIR: Path | None = None
LOG_HANDLE = None
LOG_LOCK = threading.Lock()
METRICS_PATH: Path | None = None

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def set_verbose(v: bool) -> None:
    global VERBOSE
    VERBOSE = v


def set_log_target(path: Path | None) -> None:
    global LOG_HANDLE
    if LOG_HANDLE is not None:
        try:
            LOG_HANDLE.close()
        except Exception:
            pass
        LOG_HANDLE = None
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    LOG_HANDLE = path.open("a", encoding="utf-8", buffering=1)


def write_log_line(line: str) -> None:
    if LOG_HANDLE is None:
        return
    with LOG_LOCK:
        LOG_HANDLE.write(line + "\n")


def close_log_target() -> None:
    global LOG_HANDLE
    if LOG_HANDLE is not None:
        try:
            LOG_HANDLE.close()
        except Exception:
            pass
        LOG_HANDLE = None


def write_metric(data: dict) -> None:
    if METRICS_PATH is None:
        return
    try:
        with open(METRICS_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False) + "\n")
    except Exception:
        pass


def write_raw_output(output: str) -> None:
    if LOG_HANDLE is None or not output.strip():
        return
    with LOG_LOCK:
        LOG_HANDLE.write("\n===== RAW_OUTPUT =====\n")
        LOG_HANDLE.write(output)
        if not output.endswith("\n"):
            LOG_HANDLE.write("\n")


def _wait_writable(fd: int) -> None:
    try:
        select.select([], [fd], [], 1.0)
    except (OSError, ValueError):
        pass


def _safe_write_fd(fd: int, data: bytes, deadline_sec: float = 10.0) -> bool:
    """Chunked os.write that survives O_NONBLOCK pipes. Returns False when
    the reader is gone (deadline expired or fd error) so callers can stop
    paying the deadline on every subsequent line."""
    view = memoryview(data)
    deadline = time.monotonic() + deadline_sec
    while view:
        try:
            n = os.write(fd, view)
            if n == 0:
                if time.monotonic() >= deadline:
                    return False
                _wait_writable(fd)
                continue
            view = view[n:]
        except BlockingIOError:
            if time.monotonic() >= deadline:
                return False
            _wait_writable(fd)
        except OSError:
            return False
    return True


_STDERR_GAVE_UP = False


def _safe_write_stderr(line: str) -> None:
    """Write to stderr handling non-blocking pipes (CI runners set O_NONBLOCK).

    Big bursts (e.g. dumping captured stage stdout that contains Claude's
    streaming JSON with kilobyte-sized `thinking.signature` blocks) overflow
    the pipe buffer and a plain `print()` raises BlockingIOError. We chunk +
    poll with select() until the OS accepts the bytes; if the reader is gone
    we drop output instead of crashing or spinning forever. Lines always
    reach the round log regardless (eprint/log write it separately).
    """
    global _STDERR_GAVE_UP
    if _STDERR_GAVE_UP:
        return
    data = (line + "\n").encode("utf-8", errors="replace")
    try:
        fd = sys.stderr.fileno()
    except Exception:
        # stderr replaced by a file-like without a real fd (pytest capture,
        # embedding): degrade to a plain buffered write.
        try:
            sys.stderr.write(line + "\n")
        except Exception:
            pass
        return
    if not _safe_write_fd(fd, data):
        _STDERR_GAVE_UP = True


def _safe_write_stdout(text: str) -> None:
    """Same contract as _safe_write_stderr, for stdout (verbose stage dumps)."""
    data = (text + "\n").encode("utf-8", errors="replace")
    try:
        sys.stdout.flush()
        fd = sys.stdout.fileno()
    except Exception:
        try:
            sys.stdout.write(text + "\n")
        except Exception:
            pass
        return
    _safe_write_fd(fd, data)


def eprint(msg: str) -> None:
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    _safe_write_stderr(line)
    write_log_line(ANSI_RE.sub("", line))


def init_log_file() -> None:
    global LOG_DIR, METRICS_PATH
    start_ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    LOG_DIR = Path(".medulla") / "logs" / start_ts
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    METRICS_PATH = LOG_DIR / "metrics.jsonl"
    set_log_target(LOG_DIR / "round_000_boot.log")
    eprint("")
    eprint(f"[medulla] log_dir={LOG_DIR}")


CURRENT_ROUND: int = 0
CURRENT_STAGE: str = ""


def set_round_log_file(round_no: int, stage: str) -> None:
    global CURRENT_ROUND, CURRENT_STAGE
    CURRENT_ROUND = round_no
    CURRENT_STAGE = stage
    if LOG_DIR is None:
        return
    safe_stage = re.sub(r"[^a-zA-Z0-9_-]+", "_", stage).strip("_") or "stage"
    set_log_target(LOG_DIR / f"round_{round_no:03d}_{safe_stage}.log")


def write_prompt_file(prompt: str) -> Path | None:
    import threading
    if LOG_DIR is None:
        return None
    safe_stage = re.sub(r"[^a-zA-Z0-9_-]+", "_", CURRENT_STAGE).strip("_") or "stage"
    tid = threading.get_ident() % 100000
    path = LOG_DIR / f"round_{CURRENT_ROUND:03d}_{safe_stage}_{tid}_prompt.md"
    path.write_text(prompt, encoding="utf-8")
    return path


def log(msg: str) -> None:
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] [medulla] {msg}"
    if VERBOSE:
        _safe_write_stderr(line)
    write_log_line(line)


def ansi(text: str, color: str) -> str:
    if os.environ.get("NO_COLOR"):
        return text
    return f"{color}{text}{RESET}"


def round_banner(
    round_no: int,
    stage: str,
    executor: str,
    model: str | None,
    iteration: int | None = None,
    loop_index: int | None = None,
    loop_total: int | None = None,
    loop_item: str | None = None,
) -> str:
    engine = f"{executor}:{model}" if model else executor
    left = ansi("-----", DIM)
    title = ansi(f"ROUND {round_no}", BOLD + CYAN)
    it = f" it:{iteration}" if iteration else ""
    payload = f"[{ansi(stage, YELLOW)}:{ansi(engine, GREEN)}{it}]"
    if loop_index is not None and loop_total is not None and loop_item is not None:
        max_len = 64
        display_item = loop_item if len(loop_item) <= max_len else loop_item[:max_len - 1] + "…"
        loop_suffix = f" ── loop {ansi(f'{loop_index}/{loop_total}', BOLD + CYAN)}: {ansi(display_item, DIM)}"
    else:
        loop_suffix = ""
    right = ansi("------", DIM)
    return f"{left} {title} {payload}{loop_suffix} {right}"


def round_stats(next_stage: str, duration_s: float, avg_s: float) -> str:
    label = ansi("round_finish", BOLD + GREEN)
    nxt = ansi(f"next={next_stage}", YELLOW)
    current = ansi(f"t={duration_s:.2f}s", CYAN)
    avg = ansi(f"avg={avg_s:.2f}s", CYAN)
    return f"{label}: {nxt} {current} {avg}"


def format_signal_line(sig_name: str, attrs: dict[str, str], body: str) -> str:
    detail = (body or "").strip()
    color = GREEN
    if sig_name in ("failed", "rejected"):
        color = RED
    elif sig_name == "update":
        color = CYAN
    elif sig_name == "var":
        color = YELLOW
    prefix = ansi(f"signal:{sig_name}", BOLD + color)
    if sig_name == "var":
        key_attr = attrs.get("key", "")
        return f"{prefix} {key_attr}={detail}".strip()
    if detail:
        return f"{prefix} {detail}"
    return prefix
