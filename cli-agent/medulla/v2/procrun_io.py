"""Stoppable pipe capture with detached best-effort log and echo delivery."""
from __future__ import annotations

import codecs
import os
import queue
import selectors
import socket
import threading
import time

STOP_GRACE_S = 0.05


def defer_reap(proc) -> bool:
    """Reap outside the caller's deadline; report whether handoff succeeded."""
    def reap() -> None:
        proc.wait()

    try:
        threading.Thread(target=reap, daemon=True, name=f"reap-{proc.pid}").start()
    except RuntimeError:
        return False
    return True


def watch_output(proc, out_buf: list, err_buf: list, deadline: float,
                 idle_timeout_s: float, first_output_s: float) -> str:
    """Wait for an agent CLI, returning why its output became unhealthy."""
    seen = 0
    last = time.monotonic()
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return ""
        now_seen = len(out_buf) + len(err_buf)
        if now_seen > seen:
            seen, last = now_seen, time.monotonic()
        quiet = time.monotonic() - last
        if seen == 0 and quiet > first_output_s:
            return f"no output at all in {first_output_s}s"
        if seen > 0 and quiet > idle_timeout_s:
            return f"silent for {idle_timeout_s}s after {seen} lines"
        time.sleep(min(0.25, max(0, deadline - time.monotonic())))
    return ""


class OutputCapture:
    """Keep routing capture independent from potentially blocking output sinks."""

    def __init__(self, proc, log_file, echo) -> None:
        self.out_buf: list[str] = []
        self.err_buf: list[str] = []
        self._log_file = log_file
        self._echo = echo
        self._log_items = queue.SimpleQueue()
        self._echo_items = queue.SimpleQueue()
        self._pipes = [(proc.stdout, self.out_buf, "out")]
        if proc.stderr is not None:
            self._pipes.append((proc.stderr, self.err_buf, "err"))
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._capture_ready = False
        self._wake_r, self._wake_w = socket.socketpair()
        self._capture = threading.Thread(target=self._read_pipes, daemon=True, name="procrun-capture")
        self._log_sink = threading.Thread(target=self._write_log, daemon=True, name="procrun-log")
        self._echo_sink = threading.Thread(target=self._write_echo, daemon=True, name="procrun-echo")
        self._capture_started = False
        self._log_started = False
        self._echo_started = False

    def start(self, deadline: float) -> bool:
        try:
            self._log_sink.start()
            self._log_started = True
            if self._echo is not None:
                self._echo_sink.start()
                self._echo_started = True
            self._capture.start()
            self._capture_started = True
            if not self._ready.wait(max(0, deadline - time.monotonic())) \
                    or not self._capture_ready:
                self.abort()
                return False
        except RuntimeError:
            self.abort()
            return False
        except BaseException:
            self.abort()
            raise
        return True

    def finish(self, drain_deadline: float) -> bool:
        """Stop capture within one drain cap; never wait for best-effort sinks."""
        was_alive = False
        try:
            if self._capture_owned():
                stop_at = max(time.monotonic(), drain_deadline - STOP_GRACE_S)
                self._capture.join(timeout=max(0, stop_at - time.monotonic()))
                was_alive = self._capture.is_alive()
                if was_alive:
                    self._stop_capture()
                    self._capture.join(
                        timeout=max(0, drain_deadline - time.monotonic()),
                    )
            return was_alive
        finally:
            self._stop_capture()
            if self._capture_owned() and self._capture.is_alive():
                self._capture.join(
                    timeout=max(0, drain_deadline - time.monotonic()),
                )
            if not self._capture_owned():
                self._finish_sinks()
                self._close_capture_resources()
            if not self._log_owned():
                self._close_log()

    def abort(self) -> None:
        """Release resources after partial thread startup without blocking."""
        self._stop_capture()
        if not self._capture_owned():
            self._finish_sinks()
            self._close_capture_resources()
        if not self._log_owned():
            self._close_log()

    def _capture_owned(self) -> bool:
        return self._capture_started or self._capture.ident is not None

    def _log_owned(self) -> bool:
        return self._log_started or self._log_sink.ident is not None

    def _echo_owned(self) -> bool:
        return self._echo_started or self._echo_sink.ident is not None

    def _stop_capture(self) -> None:
        self._stop.set()
        try:
            self._wake_w.send(b"x")
        except OSError:
            pass

    def _read_pipes(self) -> None:
        selector = None
        states = {}
        try:
            selector = selectors.DefaultSelector()
            selector.register(self._wake_r, selectors.EVENT_READ, None)
            for pipe, buf, tag in self._pipes:
                fd = pipe.fileno()
                os.set_blocking(fd, False)
                decoder = codecs.getincrementaldecoder(pipe.encoding or "utf-8")(
                    errors="replace",
                )
                states[fd] = [pipe, buf, tag, decoder, ""]
                selector.register(fd, selectors.EVENT_READ, states[fd])
            self._capture_ready = True
            self._ready.set()
            while states and not self._stop.is_set():
                for key, _mask in selector.select():
                    if key.data is None:
                        self._wake_r.recv(1)
                        continue
                    self._read_ready(selector, states, key.fd, key.data)
        finally:
            self._ready.set()
            for fd, state in states.items():
                try:
                    data = os.read(fd, 65536)
                except (BlockingIOError, OSError):
                    data = b""
                if data:
                    self._emit_text(state, state[3].decode(data))
                self._emit_text(
                    state, state[3].decode(b"", final=True), final=True,
                )
            self._finish_sinks()
            if selector:
                selector.close()
            self._close_pipes()
            self._wake_r.close()
            self._wake_w.close()

    def _read_ready(self, selector, states, fd: int, state: list) -> None:
        try:
            data = os.read(fd, 65536)
        except BlockingIOError:
            return
        if data:
            self._emit_text(state, state[3].decode(data))
            return
        self._emit_text(state, state[3].decode(b"", final=True), final=True)
        selector.unregister(fd)
        states.pop(fd, None)

    def _emit_text(self, state: list, text: str, final: bool = False) -> None:
        combined = state[4] + text
        lines = combined.splitlines(keepends=True)
        state[4] = ""
        if lines and not final and not lines[-1].endswith(("\n", "\r")):
            state[4] = lines.pop()
        for line in lines:
            state[1].append(line)
            item = (state[2], line)
            self._log_items.put(item)
            if self._echo_started:
                self._echo_items.put(item)

    def _write_log(self) -> None:
        try:
            for item in iter(self._log_items.get, None):
                if self._log_file:
                    try:
                        self._log_file.write(f"[{item[0]}] {item[1]}")
                    except (OSError, ValueError):
                        pass
        finally:
            self._close_log()

    def _write_echo(self) -> None:
        for tag, line in iter(self._echo_items.get, None):
            try:
                self._echo(tag, line)
            except Exception:
                pass

    def _close_pipes(self) -> None:
        for pipe, _buf, _tag in self._pipes:
            try:
                pipe.close()
            except (OSError, ValueError):
                pass

    def _finish_sinks(self) -> None:
        self._log_items.put(None)
        if self._echo_owned():
            self._echo_items.put(None)

    def _close_capture_resources(self) -> None:
        self._close_pipes()
        self._wake_r.close()
        self._wake_w.close()

    def _close_log(self) -> None:
        if self._log_file:
            try:
                self._log_file.close()
            except (OSError, ValueError):
                pass
            self._log_file = None
