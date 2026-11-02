"""Event selectors: which events a rule looks at.

A selector is a conjunction of filters. ``event_type`` and ``subsystem``
accept shell-style patterns (``packet.*``). ``attributes`` is exact equality.
``where`` is an expression over the event ``e``. If ``where`` references an
attribute the event does not have, the event simply does not match.
"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Any, Mapping

from crosslayer.core.contracts.expressions import Expression
from crosslayer.core.errors import ConfigError, MissingEvidence
from crosslayer.core.telemetry.events import NormalizedEvent

_SELECTOR_KEYS = {"event_type", "subsystem", "source_system", "severity", "attributes", "where"}


@dataclass(frozen=True)
class Selector:
    event_types: tuple[str, ...] = ()
    subsystems: tuple[str, ...] = ()
    source_systems: tuple[str, ...] = ()
    severities: tuple[str, ...] = ()
    attributes: tuple[tuple[str, Any], ...] = ()
    where: Expression | None = None

    def matches(self, event: NormalizedEvent) -> bool:
        if self.event_types and not any(fnmatchcase(event.event_type, p) for p in self.event_types):
            return False
        if self.subsystems and not any(fnmatchcase(event.subsystem, p) for p in self.subsystems):
            return False
        if self.source_systems and event.source_system not in self.source_systems:
            return False
        if self.severities and str(event.severity) not in self.severities:
            return False
        for key, expected in self.attributes:
            if key not in event.attributes or event.attributes[key] != expected:
                return False
        if self.where is not None:
            try:
                return bool(self.where.evaluate(e=event))
            except MissingEvidence:
                return False
        return True

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.event_types:
            out["event_type"] = list(self.event_types)
        if self.subsystems:
            out["subsystem"] = list(self.subsystems)
        if self.source_systems:
            out["source_system"] = list(self.source_systems)
        if self.severities:
            out["severity"] = list(self.severities)
        if self.attributes:
            out["attributes"] = dict(self.attributes)
        if self.where is not None:
            out["where"] = self.where.source
        return out

    def describe(self) -> str:
        parts = [f"{k}={v}" for k, v in self.to_dict().items()]
        return ", ".join(parts)


def _string_list(value: Any, location: str) -> tuple[str, ...]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not value or not all(isinstance(v, str) and v for v in value):
        raise ConfigError("must be a non-empty string or list of strings", location=location)
    return tuple(value)


def parse_selector(raw: Any, location: str) -> Selector:
    if not isinstance(raw, Mapping):
        raise ConfigError("selector must be a mapping", location=location)
    unknown = set(raw) - _SELECTOR_KEYS
    if unknown:
        raise ConfigError(
            f"unknown selector keys {sorted(unknown)} (allowed: {sorted(_SELECTOR_KEYS)})",
            location=location,
        )
    if not raw:
        raise ConfigError("selector must have at least one filter", location=location)

    attributes: tuple[tuple[str, Any], ...] = ()
    if "attributes" in raw:
        attrs = raw["attributes"]
        if not isinstance(attrs, Mapping) or not attrs:
            raise ConfigError("must be a non-empty mapping", location=f"{location}.attributes")
        attributes = tuple(sorted(attrs.items()))

    where = None
    if "where" in raw:
        where = Expression(raw["where"], names={"e"}, location=f"{location}.where")

    return Selector(
        event_types=_string_list(raw["event_type"], f"{location}.event_type") if "event_type" in raw else (),
        subsystems=_string_list(raw["subsystem"], f"{location}.subsystem") if "subsystem" in raw else (),
        source_systems=_string_list(raw["source_system"], f"{location}.source_system")
        if "source_system" in raw
        else (),
        severities=_string_list(raw["severity"], f"{location}.severity") if "severity" in raw else (),
        attributes=attributes,
        where=where,
    )
