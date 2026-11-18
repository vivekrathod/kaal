"""Running the programs that read stored files: pdftotext, pdftoppm and tesseract.

A file someone saved is untrusted. Each run has a time limit, a limit on what
it may write and, where the system allows it, a memory limit; only a few run
at once; and nothing a program says about a file (its complaints can run to
gigabytes for a file of a few kilobytes) is kept. So one bad file, or many at
once, cannot take the server down. The limits come from the environment:

    KAAL_FILE_JOBS        how many such programs may run at once (default 2)
    KAAL_FILE_MEMORY_MB   memory each may use, in MiB (default 1024; 0 for no limit)
    KAAL_FILE_OUTPUT_MB   about the most each may write to one file, in MiB (default 256; 0 for no
                          limit; on some systems the shell counts in larger blocks and allows twice this)

Showing a picture of a file in the web app takes its turn in a small pool of
its own, so that people looking at files and the reading of new receipts do
not wait on each other.
"""
from __future__ import annotations

import contextlib
import os
import subprocess
import tempfile
import threading
from collections.abc import Iterator, Sequence
from contextvars import ContextVar

# What is read back of a program's own output (the text of a file): more than any real document has.
MAX_OUTPUT_BYTES = 32 * 1024 * 1024
# How long a request waits for its turn before it is told the server is busy. The background
# reader of receipts waits longer (see ``patient``): nobody is waiting on it.
DEFAULT_WAIT_SECONDS = 20.0
PREVIEW_WAIT_SECONDS = 6.0

_pools: dict[str, threading.BoundedSemaphore] = {}
_pools_lock = threading.Lock()
_wait: ContextVar[float | None] = ContextVar("kaal_tool_wait", default=None)


class Busy(Exception):
    """No turn came up in time. Nothing was learnt about the file: try again later."""


def _env_number(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name) or default))
    except ValueError:
        return default


def _pool(name: str) -> threading.BoundedSemaphore:
    with _pools_lock:
        if name not in _pools:
            _pools[name] = threading.BoundedSemaphore(max(1, _env_number("KAAL_FILE_JOBS", 2)))
        return _pools[name]


@contextlib.contextmanager
def patient(seconds: float) -> Iterator[None]:
    """Within this, wait up to ``seconds`` for a turn instead of the usual few."""
    token = _wait.set(seconds)
    try:
        yield
    finally:
        _wait.reset(token)


@contextlib.contextmanager
def turn(pool: str | None = "files", wait: float | None = None) -> Iterator[None]:
    """One of the few turns at reading a file; ``Busy`` when none comes up in time."""
    if pool is None:
        # The caller already holds a turn (asking for a second could wait on itself for ever).
        yield
        return
    allowed = _wait.get()
    if allowed is None:
        allowed = wait if wait is not None else (PREVIEW_WAIT_SECONDS if pool == "previews" else DEFAULT_WAIT_SECONDS)
    gate = _pool(pool)
    if not gate.acquire(timeout=allowed):
        raise Busy()
    try:
        yield
    finally:
        gate.release()


def run(argv: Sequence[str], *, timeout: float, text: bool = False, pool: str | None = "files", wait: float | None = None) -> subprocess.CompletedProcess | None:
    """Run one program on a stored file. None when it timed out or could not start; ``Busy`` when
    it never got a turn, which says nothing about the file."""
    with turn(pool, wait):
        command = list(argv)
        limits = []
        memory = _env_number("KAAL_FILE_MEMORY_MB", 1024)
        if memory:
            limits.append(f"ulimit -v {memory * 1024} 2>/dev/null")
        written = _env_number("KAAL_FILE_OUTPUT_MB", 256)
        if written:
            limits.append(f"ulimit -f {written * 2048} 2>/dev/null")      # counted in 512-byte blocks
        if limits:
            # The shell sets the limits and then becomes the program. Where a limit cannot be
            # set (some systems refuse), the program runs without it.
            command = ["/bin/sh", "-c", "; ".join(limits) + '; exec "$@"', "kaal-tool", *command]
        # The program gets a bare environment: nothing that names the key, a token or a service.
        bare = {name: os.environ[name] for name in ("PATH", "LANG", "LC_ALL", "TESSDATA_PREFIX", "TMPDIR") if name in os.environ}
        try:
            # What it prints goes to a file, which the limit above bounds, never into this process;
            # what it complains about is not kept at all.
            with tempfile.TemporaryFile() as printed:
                done = subprocess.run(command, stdout=printed, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, timeout=timeout, env=bare)
                printed.seek(0)
                output = printed.read(MAX_OUTPUT_BYTES)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return subprocess.CompletedProcess(done.args, done.returncode, output.decode("utf-8", "replace") if text else output, "" if text else b"")
