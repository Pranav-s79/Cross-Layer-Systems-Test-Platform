"""A content-addressed cache of compiled Icarus simulations (``sim.vvp``).

Every run used to recompile the same RTL. The compiled image depends only on
what goes into ``iverilog`` (the exact source text after mutants, the
defines, the flags) and on the tools themselves, so it is cached under a
SHA-256 of exactly those inputs. The tools enter the key by path, size, and
modification time, so upgrading Icarus misses the cache instead of reusing an
image from another version. The program is loaded at run time (``+hex``), so
one image serves every program and seed.

Location: ``$CROSSLAYER_CACHE_DIR/rv32i-sim`` if the variable is set (``off``
disables the cache), else ``%LOCALAPPDATA%/crosslayer/rv32i-sim`` on Windows
or ``${XDG_CACHE_HOME:-~/.cache}/crosslayer/rv32i-sim`` elsewhere. Writes are
atomic (temporary file, then rename), so concurrent runs can share it. A cache
problem never fails a run: it falls back to compiling.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Sequence

ENV = "CROSSLAYER_CACHE_DIR"
_OFF = {"", "0", "off", "none", "false"}


def cache_dir() -> Path | None:
    value = os.environ.get(ENV)
    if value is not None:
        return None if value.strip().lower() in _OFF else Path(value) / "rv32i-sim"
    base = os.environ.get("LOCALAPPDATA") if os.name == "nt" else os.environ.get("XDG_CACHE_HOME")
    root = Path(base) if base else Path.home() / ".cache"
    return root / "crosslayer" / "rv32i-sim"


def _tool_identity(path: str | None) -> list[object]:
    if path is None:
        return [None]
    st = Path(path).stat()
    return [str(Path(path).resolve()), st.st_size, st.st_mtime_ns]


def compile_key(tools: Sequence[str | None], flags: Sequence[str], files: Sequence[tuple[str, str]]) -> str:
    """SHA-256 over the tools, the compiler flags, and each (name, text) compiled, in order."""
    payload = {
        "tools": [_tool_identity(t) for t in tools],
        "flags": list(flags),
        "files": [[name, hashlib.sha256(text.encode("utf-8")).hexdigest()] for name, text in files],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def fetch(key: str, target: Path) -> bool:
    """Copy a cached image to ``target``; False on a miss or any cache problem."""
    root = cache_dir()
    if root is None:
        return False
    try:
        shutil.copyfile(root / f"{key}.vvp", target)
        return True
    except OSError:
        return False


def store(key: str, image: Path) -> None:
    """Add a compiled image under ``key``, atomically; problems are ignored."""
    root = cache_dir()
    if root is None:
        return
    try:
        root.mkdir(parents=True, exist_ok=True)
        tmp = root / f"{key}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        shutil.copyfile(image, tmp)
        os.replace(tmp, root / f"{key}.vvp")
    except OSError:
        pass
