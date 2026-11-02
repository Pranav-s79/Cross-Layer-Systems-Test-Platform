"""The normalized event: the only data shape the contract engine sees.

Every adapter converts its raw output (simulator traces, RAN logs, firmware
UART dumps, ...) into ``NormalizedEvent`` records. The core never parses a
vendor format itself.

Design rules enforced here:

* ``timestamp_ns`` is an integer nanosecond count. Floats lose sub-microsecond
  precision at epoch-scale magnitudes, which matters when ordering events from
  different components.
* Attribute keys must be namespaced (``rtl.pc``, ``ran.rnti``,
  ``pipeline.latency_us``). Domain measurements stay in their own namespace
  instead of being forced into one universal schema.
* Correlation ids are explicit ``{kind: id}`` pairs (``{"ue": "1"}``) so rules
  can join events across components without guessing.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping

_EVENT_TYPE_RE = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)*$")
_ATTRIBUTE_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$")
_CORRELATION_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class Severity(StrEnum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


def _check_json_value(value: Any, where: str) -> None:
    """Attributes must survive a JSON round trip unchanged."""
    if value is None or isinstance(value, (bool, int, str)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{where}: non-finite float {value!r} is not JSON-safe")
        return
    if isinstance(value, (list, tuple)):
        for i, item in enumerate(value):
            _check_json_value(item, f"{where}[{i}]")
        return
    if isinstance(value, dict):
        for k, item in value.items():
            if not isinstance(k, str):
                raise ValueError(f"{where}: nested keys must be strings")
            _check_json_value(item, f"{where}.{k}")
        return
    raise ValueError(f"{where}: unsupported attribute type {type(value).__name__}")


@dataclass(frozen=True, slots=True)
class NormalizedEvent:
    run_id: str
    event_id: str
    """Unique within a run. Evidence in reports points back to this id."""
    source_system: str
    """Which system under test produced the event (e.g. ``ocudu``, ``rv32i``)."""
    subsystem: str
    """Component inside that system (e.g. ``du``, ``amf``, ``fetch``)."""
    timestamp_ns: int
    event_type: str
    severity: Severity = Severity.INFO
    correlation_ids: Mapping[str, str] = field(default_factory=dict)
    attributes: Mapping[str, Any] = field(default_factory=dict)
    raw_artifact_ref: str | None = None
    """Where the raw evidence lives, e.g. ``raw_artifacts/gnb.log#L1042``."""
    confidence_metadata: Mapping[str, Any] = field(default_factory=dict)
    """How trustworthy the normalization is (timestamp source, inferred fields...)."""

    def __post_init__(self) -> None:
        for name in ("run_id", "event_id", "source_system", "subsystem"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"event.{name} must be a non-empty string, got {value!r}")

        if not isinstance(self.event_type, str) or not _EVENT_TYPE_RE.match(self.event_type):
            raise ValueError(
                f"event.event_type {self.event_type!r} must be dotted lowercase, e.g. 'packet.received'"
            )

        if isinstance(self.timestamp_ns, bool) or not isinstance(self.timestamp_ns, int):
            raise ValueError(f"event.timestamp_ns must be an int, got {type(self.timestamp_ns).__name__}")
        if self.timestamp_ns < 0:
            raise ValueError("event.timestamp_ns must be >= 0")

        object.__setattr__(self, "severity", Severity(self.severity))

        ids: dict[str, str] = {}
        for key, value in dict(self.correlation_ids).items():
            if not isinstance(key, str) or not _CORRELATION_KEY_RE.match(key):
                raise ValueError(f"correlation id key {key!r} must be lowercase snake_case")
            if isinstance(value, bool) or not isinstance(value, (str, int)):
                raise ValueError(f"correlation id {key!r} must be str or int, got {value!r}")
            ids[key] = str(value)
        object.__setattr__(self, "correlation_ids", ids)

        attrs = dict(self.attributes)
        for key, value in attrs.items():
            if not isinstance(key, str) or not _ATTRIBUTE_KEY_RE.match(key):
                raise ValueError(
                    f"attribute key {key!r} must be namespaced lowercase, e.g. 'rtl.pc'"
                )
            _check_json_value(value, f"attributes[{key!r}]")
        object.__setattr__(self, "attributes", attrs)

        object.__setattr__(self, "confidence_metadata", dict(self.confidence_metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "event_id": self.event_id,
            "source_system": self.source_system,
            "subsystem": self.subsystem,
            "timestamp_ns": self.timestamp_ns,
            "event_type": self.event_type,
            "severity": str(self.severity),
            "correlation_ids": dict(self.correlation_ids),
            "attributes": dict(self.attributes),
            "raw_artifact_ref": self.raw_artifact_ref,
            "confidence_metadata": dict(self.confidence_metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "NormalizedEvent":
        known = {
            "run_id", "event_id", "source_system", "subsystem", "timestamp_ns",
            "event_type", "severity", "correlation_ids", "attributes",
            "raw_artifact_ref", "confidence_metadata",
        }
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"unknown event fields: {sorted(unknown)}")
        return cls(**data)
