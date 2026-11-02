"""JSONL persistence, ordering, and fingerprinting for normalized events."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Iterable, Sequence

from crosslayer.core.telemetry.events import NormalizedEvent


def canonical_json(obj: object) -> str:
    """Deterministic JSON: sorted keys, no whitespace. Used for hashing."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def order_events(events: Iterable[NormalizedEvent]) -> list[NormalizedEvent]:
    """Sort by timestamp. Python's sort is stable, so events with equal
    timestamps keep the order the adapter emitted them in (usually log order),
    which is the best available tie-breaker."""
    return sorted(events, key=lambda e: e.timestamp_ns)


def write_events_jsonl(path: Path, events: Sequence[NormalizedEvent]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for event in events:
            fh.write(canonical_json(event.to_dict()))
            fh.write("\n")


def read_events_jsonl(path: Path) -> list[NormalizedEvent]:
    events: list[NormalizedEvent] = []
    with path.open("r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            try:
                events.append(NormalizedEvent.from_dict(json.loads(line)))
            except (ValueError, TypeError) as exc:
                raise ValueError(f"{path}:{lineno}: {exc}") from exc
    return events


def functional_fingerprint(events: Sequence[NormalizedEvent]) -> str:
    """SHA-256 over the event stream with run-specific fields removed.

    Two runs of a deterministic system with the same seed and configuration
    should produce the same fingerprint. That is how CrossLayer checks
    "identical-seed functional repeatability". ``run_id`` is excluded because
    it is unique per run by construction.
    """
    digest = hashlib.sha256()
    for event in events:
        data = event.to_dict()
        data.pop("run_id")
        digest.update(canonical_json(data).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()
