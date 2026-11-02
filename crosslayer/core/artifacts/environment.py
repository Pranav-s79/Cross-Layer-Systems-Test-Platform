"""Capture the environment a run executed in, for versions.json."""

from __future__ import annotations

import platform
import subprocess
from pathlib import Path
from typing import Any

import crosslayer


def _git(args: list[str], cwd: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def git_info(path: Path) -> dict[str, Any] | None:
    """Revision and dirty flag of the repository containing ``path``.

    A dirty tree means the bundle cannot be reproduced from the revision
    alone, so the flag is recorded explicitly.
    """
    revision = _git(["rev-parse", "HEAD"], path)
    if revision is None:
        return None
    status = _git(["status", "--porcelain"], path)
    return {
        "revision": revision,
        "branch": _git(["rev-parse", "--abbrev-ref", "HEAD"], path),
        "dirty": bool(status),
    }


def capture_environment() -> dict[str, Any]:
    source_root = Path(crosslayer.__file__).resolve().parent.parent
    return {
        "crosslayer_version": crosslayer.__version__,
        "crosslayer_git": git_info(source_root),
        "python": {
            "version": platform.python_version(),
            "implementation": platform.python_implementation(),
        },
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
    }
