from __future__ import annotations

import itertools
from typing import Any

import pytest

from crosslayer.core.telemetry.events import NormalizedEvent

_ids = itertools.count(1)


def ev(
    event_type: str,
    t: int,
    *,
    subsystem: str = "sub",
    ids: dict[str, Any] | None = None,
    attrs: dict[str, Any] | None = None,
    severity: str = "info",
    source: str = "test",
) -> NormalizedEvent:
    """Compact event factory for rule tests. ``t`` is in nanoseconds."""
    n = next(_ids)
    return NormalizedEvent(
        run_id="run-test",
        event_id=f"e{n}",
        source_system=source,
        subsystem=subsystem,
        timestamp_ns=t,
        event_type=event_type,
        severity=severity,
        correlation_ids=ids or {},
        attributes=attrs or {},
        raw_artifact_ref=f"raw_artifacts/test.log#L{n}",
    )


@pytest.fixture
def make_event():
    return ev
