"""Running the programs that read stored files: pdftotext, pdftoppm and tesseract.

A file someone saved is untrusted. Each run has a time limit and, where the
system allows it, a memory limit, and only a few run at once, so one bad file
(or many at once) cannot take the server down. The limits come from the
environment:

    KAAL_FILE_JOBS        how many such programs may run at once (default 2)
    KAAL_FILE_MEMORY_MB   memory each may use, in MiB (default 1024; 0 for no limit)
"""
from __future__ import annotations

import os
import subprocess
import threading
from collections.abc import Sequence

_jobs: threading.BoundedSemaphore | None = None
_jobs_lock = threading.Lock()


def _gate() -> threading.BoundedSemaphore:
    global _jobs
    with _jobs_lock:
        if _jobs is None:
            _jobs = threading.BoundedSemaphore(max(1, int(os.environ.get("KAAL_FILE_JOBS") or 2)))
        return _jobs


def _memory_kib() -> int:
    try:
        return max(0, int(os.environ.get("KAAL_FILE_MEMORY_MB") or 1024)) * 1024
    except ValueError:
        return 1024 * 1024


def run(argv: Sequence[str], *, timeout: float, text: bool = False, wait: float = 120.0) -> subprocess.CompletedProcess | None:
    """Run one program on a stored file. None when it timed out, could not start, or had to wait too long for a turn."""
    gate = _gate()
    if not gate.acquire(timeout=wait):
        return None
    try:
        command = list(argv)
        limit = _memory_kib()
        if limit:
            # The shell sets the memory limit and then becomes the program. Where the
            # limit cannot be set (some systems refuse), the program runs without one.
            command = ["/bin/sh", "-c", f'ulimit -v {limit} 2>/dev/null; exec "$@"', "kaal-tool", *command]
        # The program gets a bare environment: nothing that names the key, a token or a service.
        bare = {name: os.environ[name] for name in ("PATH", "LANG", "LC_ALL", "TESSDATA_PREFIX", "TMPDIR") if name in os.environ}
        return subprocess.run(command, capture_output=True, text=text, timeout=timeout, stdin=subprocess.DEVNULL, env=bare)
    except (OSError, subprocess.TimeoutExpired):
        return None
    finally:
        gate.release()
