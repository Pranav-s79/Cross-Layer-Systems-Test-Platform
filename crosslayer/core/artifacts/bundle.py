"""The reproducible run bundle: one self-contained directory per run.

::

    <run_id>/
    ├── manifest.json            index of everything below, with SHA-256 per file
    ├── configuration.yaml       resolved experiment (contracts inlined)
    ├── versions.json            platform, adapter, and system-under-test versions
    ├── raw_artifacts/           untouched output of the system under test
    ├── normalized_events.jsonl  what the contracts were evaluated against
    ├── normalization.json       parse diagnostics (raw lines that did not normalize)
    ├── contracts.json           every contract and its result
    ├── violations.json          failing contracts with evidence only
    ├── timings.json             seconds per runner stage
    ├── report.md                human-readable summary
    ├── report.html              interactive, step-by-step summary
    ├── junit.xml                CI-compatible results
    └── replay.sh                re-run this exact experiment
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

from crosslayer.core.errors import ConfigError

BUNDLE_FORMAT_VERSION = 1
MANIFEST = "manifest.json"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


class RunBundle:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.raw_dir = root / "raw_artifacts"
        self.work_dir = root / "work"

    @classmethod
    def create(cls, output_root: Path, run_id: str) -> "RunBundle":
        root = output_root / run_id
        root.mkdir(parents=True, exist_ok=False)
        bundle = cls(root)
        bundle.raw_dir.mkdir()
        bundle.work_dir.mkdir()
        return bundle

    def path(self, name: str) -> Path:
        return self.root / name

    def write_text(self, name: str, text: str) -> Path:
        path = self.path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        return path

    def write_json(self, name: str, obj: Any) -> Path:
        return self.write_text(name, json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n")

    def write_yaml(self, name: str, obj: Any) -> Path:
        return self.write_text(name, yaml.safe_dump(obj, sort_keys=False, allow_unicode=True))

    def file_index(self) -> list[dict[str, Any]]:
        """Every bundle file except the manifest itself and scratch ``work/``."""
        entries = []
        for path in sorted(self.root.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(self.root)
            if rel.as_posix() == MANIFEST or rel.parts[0] == "work":
                continue
            entries.append({"path": rel.as_posix(), "bytes": path.stat().st_size, "sha256": sha256_file(path)})
        return entries


def load_json(bundle_dir: Path, name: str) -> Any:
    return json.loads((bundle_dir / name).read_text(encoding="utf-8"))


def load_manifest(bundle_dir: Path) -> dict[str, Any]:
    """Read a bundle's manifest, with a clear error if this is not a bundle."""
    path = bundle_dir / MANIFEST
    if not path.is_file():
        raise ConfigError(f"not a run bundle (no {MANIFEST})", location=str(bundle_dir))
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"unreadable manifest: {exc}", location=str(path)) from exc


def verify_bundle(bundle_dir: Path) -> list[str]:
    """Compare the bundle on disk with its manifest.

    Reports files that are missing, modified (hash differs), or unexpected
    (present on disk but not in the manifest, e.g. added afterwards).
    """
    manifest = load_manifest(bundle_dir)
    problems = []
    listed = set()
    for entry in manifest.get("files", []):
        listed.add(entry["path"])
        path = bundle_dir / entry["path"]
        if not path.is_file():
            problems.append(f"missing: {entry['path']}")
        elif sha256_file(path) != entry["sha256"]:
            problems.append(f"modified: {entry['path']}")
    on_disk = {e["path"] for e in RunBundle(bundle_dir).file_index()}
    problems += [f"unexpected: {p}" for p in sorted(on_disk - listed)]
    return problems
