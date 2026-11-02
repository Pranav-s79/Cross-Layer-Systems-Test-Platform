"""Run a system-under-test process with a hard timeout.

The child gets its own process group (POSIX session / Windows process group)
so a timeout kills the whole tree, including any helpers it spawned. Plain
``subprocess.run(timeout=...)`` only kills the direct child.

Durations use ``time.monotonic()``: wall-clock time can jump (NTP, DST) and
would corrupt timing measurements.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


@dataclass
class ProcessResult:
    command: list[str]
    exit_code: int | None
    timed_out: bool
    duration_s: float
    error: str | None = None
    """Set when the process could not be started."""


def _kill_tree(proc: subprocess.Popen[bytes]) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if proc.poll() is None:
        proc.kill()


def run_process(
    command: Sequence[str],
    *,
    cwd: Path,
    timeout_s: float,
    stdout_path: Path,
    stderr_path: Path,
    env_overrides: Mapping[str, str] | None = None,
) -> ProcessResult:
    env = dict(os.environ)
    if env_overrides:
        env.update(env_overrides)

    group_kwargs: dict[str, object] = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}  # type: ignore[attr-defined]
        if os.name == "nt"
        else {"start_new_session": True}
    )

    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path.parent.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    with stdout_path.open("wb") as out, stderr_path.open("wb") as err:
        try:
            proc = subprocess.Popen(
                list(command),
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                env=env,
                **group_kwargs,  # type: ignore[arg-type]
            )
        except OSError as exc:
            return ProcessResult(list(command), None, False, time.monotonic() - start, error=str(exc))

        try:
            exit_code: int | None = proc.wait(timeout=timeout_s)
            timed_out = False
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
            exit_code = None
            timed_out = True

    return ProcessResult(list(command), exit_code, timed_out, time.monotonic() - start)
