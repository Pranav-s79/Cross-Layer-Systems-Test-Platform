"""Log profiles: a declarative description of one line-oriented text log format.

A profile is a YAML file that maps line regexes to event types, attributes
and correlation ids, and says how to read the timestamp. Built-in profiles
live in ``profiles/<name>.yaml``; an experiment can also point a source at
any profile file, so a new log format needs no Python changes.

The profile describes the *format*. Facts about one capture (the host's UTC
offset, the year of a year-less timestamp, a known clock offset) can also be
given per source in the experiment, which overrides the profile.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Collection, Mapping

import yaml

from crosslayer.core.errors import ConfigError
from crosslayer.core.telemetry.events import NormalizedEvent, Severity
from crosslayer.core.units import NS_PER_S

PROFILES_DIR = Path(__file__).resolve().parent / "profiles"
SCHEMA_VERSION = 1
SOURCE_ATTRIBUTE = "log.source"
"""Attribute set on every event: the name of the profile that parsed it."""
ATTRIBUTE_TYPES = ("str", "int", "hex", "float")
ID_TYPES = ("str", "int", "hex")
UNMATCHED = ("diagnostic", "ignore")

_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_SUBSYSTEM_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
_OFFSET_RE = re.compile(r"^([+-])(\d{2}):(\d{2})$")
_TOP_KEYS = {
    "schema_version", "name", "description", "source_system", "subsystem", "timestamp", "clock",
    "skip", "unmatched", "derived_correlation", "rules",
}
_REQUIRED = ("schema_version", "name", "source_system", "subsystem", "timestamp", "clock", "rules")
_TIMESTAMP_KEYS = {"group", "format", "utc_offset", "year"}
_RULE_KEYS = {"name", "description", "match", "event_type", "subsystem", "severity", "attributes", "correlation"}
_FIELD_KEYS = {"group", "type"}
_YEAR_DIRECTIVES = frozenset("YyG")
_UNSUPPORTED_DIRECTIVES = {"Z": "time zone names are ambiguous; use utc_offset or %z",
                           "c": "locale-dependent", "x": "locale-dependent", "X": "locale-dependent"}
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


@dataclass(frozen=True)
class Field:
    """A regex group converted to a typed value."""

    group: str
    type: str = "str"

    def convert(self, text: str) -> str | int | float:
        if self.type == "int":
            return int(text, 10)
        if self.type == "hex":
            return int(text, 16)
        if self.type == "float":
            value = float(text)
            if not math.isfinite(value):
                raise ValueError("not a finite number")
            return value
        return text


@dataclass(frozen=True)
class Rule:
    name: str
    pattern: re.Pattern[str]
    event_type: str
    subsystem: str
    severity: Severity
    attributes: Mapping[str, Field]
    correlation: Mapping[str, Field]
    description: str = ""


@dataclass(frozen=True)
class TimestampSpec:
    group: str
    format: str
    utc_offset: str | None
    """``+HH:MM`` default for the profile; None if the format carries ``%z`` or the source must say."""
    year: int | None
    """Default year for formats without one; None if the format has a year or the source must say."""

    @property
    def has_year(self) -> bool:
        return bool(_directives(self.format) & _YEAR_DIRECTIVES)

    @property
    def has_zone(self) -> bool:
        return "z" in _directives(self.format)


@dataclass(frozen=True)
class TimestampParser:
    """Turns one source's timestamp text into nanoseconds since the Unix epoch (UTC)."""

    format: str
    utc_offset: str | None
    year: int | None

    def parse(self, text: str) -> int:
        """Raise ``ValueError`` if ``text`` does not match the format."""
        if self.year is not None:  # prepend the year: strptime cannot default it safely (Feb 29)
            moment = datetime.strptime(f"{self.year} {text}", f"%Y {self.format}")
        else:
            moment = datetime.strptime(text, self.format)
        if moment.tzinfo is None:
            if self.utc_offset is None:
                raise ValueError("no UTC offset known for this timestamp")
            moment = moment.replace(tzinfo=_timezone(self.utc_offset))
        delta = moment - _EPOCH
        ns = (delta.days * 86_400 + delta.seconds) * NS_PER_S + delta.microseconds * 1_000
        if ns < 0:
            raise ValueError("timestamp is before 1970-01-01T00:00:00Z")
        return ns


@dataclass(frozen=True)
class ParsedLine:
    rule: Rule
    timestamp_text: str
    attributes: dict[str, Any]
    correlation: dict[str, str]


@dataclass(frozen=True)
class LogProfile:
    name: str
    path: Path
    builtin: bool
    description: str
    source_system: str
    subsystem: str
    timestamp: TimestampSpec
    clock: Mapping[str, Any]
    """Free-form metadata about the log's clock, copied into each event's confidence_metadata."""
    rules: tuple[Rule, ...]
    derived_correlation: tuple[str, ...] = ()
    """Correlation ids added by a configured post-parse join, for event-catalog linting."""
    skip: tuple[re.Pattern[str], ...] = ()
    unmatched: str = "diagnostic"
    sha256: str = field(default="", compare=False)

    @property
    def subsystems(self) -> set[str]:
        return {self.subsystem, *(r.subsystem for r in self.rules)}

    def skips(self, line: str) -> bool:
        return any(p.search(line) for p in self.skip)

    def match(self, line: str) -> ParsedLine | str | None:
        """The first matching rule's values, an error message, or None if no rule matches."""
        for rule in self.rules:
            m = rule.pattern.search(line)
            if m is None:
                continue
            stamp = m.group(self.timestamp.group)
            if stamp is None:
                return f"rule {rule.name!r}: timestamp group {self.timestamp.group!r} did not match"
            values: dict[str, Any] = {}
            ids: dict[str, str] = {}
            for key, spec in rule.attributes.items():
                text = m.group(spec.group)
                if text is not None:  # None: an optional group that did not take part in the match
                    try:
                        values[key] = spec.convert(text)
                    except ValueError:
                        return f"rule {rule.name!r}: attribute {key!r} = {text!r} is not {spec.type}"
            for key, spec in rule.correlation.items():
                text = m.group(spec.group)
                if text is not None:
                    try:
                        ids[key] = str(spec.convert(text))  # hex ids become decimal, so formats can join
                    except ValueError:
                        return f"rule {rule.name!r}: correlation id {key!r} = {text!r} is not {spec.type}"
            return ParsedLine(rule, stamp, values, ids)
        return None


def builtin_profiles() -> list[str]:
    return sorted(p.stem for p in PROFILES_DIR.glob("*.yaml"))


def profile_path(profile: str, base_dir: Path | None = None, loc: str | None = None) -> tuple[Path, bool]:
    """Resolve a built-in profile name or a path to a profile file; True if built in."""
    if _NAME_RE.match(profile) and (PROFILES_DIR / f"{profile}.yaml").is_file():
        return PROFILES_DIR / f"{profile}.yaml", True
    path = Path(profile)
    if not path.is_absolute() and base_dir is not None:
        path = base_dir / path
    if path.suffix not in (".yaml", ".yml") or not path.is_file():
        raise ConfigError(f"unknown profile {profile!r} (built-in: {builtin_profiles()}, or a path to a .yaml file)",
                          location=loc)
    return path.resolve(), False


def load_profile(
    profile: str, base_dir: Path | None = None, *, subsystems: Collection[str] = (), loc: str | None = None,
) -> LogProfile:
    """Load and validate a profile. A non-empty ``subsystems`` restricts the
    subsystems a profile from a path may use (built-in profiles define them).
    ``loc`` names the setting that selected the profile, for errors."""
    path, builtin = profile_path(profile, base_dir, loc)
    where = str(path)
    data = path.read_bytes()
    try:
        doc = yaml.safe_load(data.decode("utf-8"))
    except (yaml.YAMLError, UnicodeDecodeError) as exc:
        raise ConfigError(f"invalid YAML: {exc}", location=where) from None
    if not isinstance(doc, dict):
        raise ConfigError("must be a mapping", location=where)
    _keys(doc, _TOP_KEYS, where)
    for key in _REQUIRED:
        if key not in doc:
            raise ConfigError("is required", location=f"{where}: {key}")
    if doc["schema_version"] != SCHEMA_VERSION:
        raise ConfigError(f"unsupported schema_version (expected {SCHEMA_VERSION})", location=f"{where}: schema_version")
    name = _str(doc["name"], f"{where}: name")
    if not _NAME_RE.match(name):
        raise ConfigError("must be lowercase letters, digits, '_' or '-'", location=f"{where}: name")
    if builtin and name != path.stem:
        raise ConfigError(f"must match the file name {path.stem!r}", location=f"{where}: name")
    description = doc.get("description") or ""
    if not isinstance(description, str):
        raise ConfigError("must be a string", location=f"{where}: description")
    source_system = _str(doc["source_system"], f"{where}: source_system")
    allowed = set() if builtin else set(subsystems)
    subsystem = _subsystem(doc["subsystem"], allowed, f"{where}: subsystem")
    timestamp = _timestamp(doc["timestamp"], f"{where}: timestamp")
    clock = doc["clock"]
    if not isinstance(clock, dict) or not clock:
        raise ConfigError("must be a non-empty mapping describing the log's clock", location=f"{where}: clock")
    clock = _json_safe(clock, f"{where}: clock")
    skip = [_regex(p, f"{where}: skip[{i}]") for i, p in enumerate(_list(doc.get("skip", []), f"{where}: skip"))]
    unmatched = doc.get("unmatched", "diagnostic")
    if unmatched not in UNMATCHED:
        raise ConfigError(f"must be one of {list(UNMATCHED)}", location=f"{where}: unmatched")
    derived_correlation = _list(doc.get("derived_correlation", []), f"{where}: derived_correlation")
    for i, key in enumerate(derived_correlation):
        if not isinstance(key, str):
            raise ConfigError("must be a correlation id name", location=f"{where}: derived_correlation[{i}]")
        _probe(f"{where}: derived_correlation[{i}]", correlation_ids={key: "0"})
    if len(set(derived_correlation)) != len(derived_correlation):
        raise ConfigError("must not repeat a correlation id", location=f"{where}: derived_correlation")

    raw_rules = _list(doc["rules"], f"{where}: rules")
    if not raw_rules:
        raise ConfigError("must list at least one rule", location=f"{where}: rules")
    rules: list[Rule] = []
    for i, raw in enumerate(raw_rules):
        rule = _rule(raw, f"{where}: rules[{i}]", timestamp.group, subsystem, allowed)
        if any(r.name == rule.name for r in rules):
            raise ConfigError(f"duplicate rule name {rule.name!r}", location=f"{where}: rules[{i}].name")
        rules.append(rule)

    return LogProfile(
        name=name, path=path, builtin=builtin, description=description.strip(), source_system=source_system,
        subsystem=subsystem, timestamp=timestamp, clock=clock, rules=tuple(rules), skip=tuple(skip),
        derived_correlation=tuple(derived_correlation), unmatched=unmatched,
        sha256=hashlib.sha256(data).hexdigest()[:12],
    )


def timestamp_parser(profile: LogProfile, utc_offset: str | None, year: int | None, loc: str) -> TimestampParser:
    """Combine a profile's timestamp spec with a source's overrides, or raise ``ConfigError``."""
    spec = profile.timestamp
    if spec.has_zone:
        if utc_offset is not None:
            raise ConfigError(f"profile {profile.name!r} reads the offset from the timestamp (%z)",
                              location=f"{loc}.utc_offset")
        offset = None
    else:
        offset = utc_offset if utc_offset is not None else spec.utc_offset
        if offset is None:
            raise ConfigError(f"is required: profile {profile.name!r} does not set timestamp.utc_offset",
                              location=f"{loc}.utc_offset")
    if spec.has_year:
        if year is not None:
            raise ConfigError(f"profile {profile.name!r} reads the year from the timestamp", location=f"{loc}.year")
        return TimestampParser(spec.format, offset, None)
    chosen = year if year is not None else spec.year
    if chosen is None:
        raise ConfigError(f"is required: profile {profile.name!r} has timestamps without a year and no timestamp.year",
                          location=f"{loc}.year")
    return TimestampParser(spec.format, offset, chosen)


def check_utc_offset(value: Any, loc: str) -> str:
    text = _str(value, loc)
    m = _OFFSET_RE.match(text)
    if m is None or int(m[2]) > 23 or int(m[3]) > 59:
        raise ConfigError(f"must be a fixed offset like '+00:00' or '-05:30', got {text!r}", location=loc)
    return text


def check_year(value: Any, loc: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1970 <= value <= 9999:
        raise ConfigError("must be a year between 1970 and 9999", location=loc)
    return value


# -- validation helpers -------------------------------------------------------------------


def _timezone(offset: str) -> timezone:
    m = _OFFSET_RE.match(offset)
    assert m is not None
    delta = timedelta(hours=int(m[2]), minutes=int(m[3]))
    return timezone(-delta if m[1] == "-" else delta)


def _directives(fmt: str) -> set[str]:
    found, i = set(), 0
    while i < len(fmt):
        if fmt[i] == "%":
            found.add(fmt[i + 1] if i + 1 < len(fmt) else "")
            i += 2
        else:
            i += 1
    found.discard("%")
    return found


def _timestamp(value: Any, loc: str) -> TimestampSpec:
    if not isinstance(value, dict):
        raise ConfigError("must be a mapping with group and format", location=loc)
    _keys(value, _TIMESTAMP_KEYS, loc)
    group = _str(value.get("group"), f"{loc}.group")
    fmt = _str(value.get("format"), f"{loc}.format")
    directives = _directives(fmt)
    if "" in directives:
        raise ConfigError("ends with a lone '%'", location=f"{loc}.format")
    for d in sorted(directives & set(_UNSUPPORTED_DIRECTIVES)):
        raise ConfigError(f"%{d} is not supported ({_UNSUPPORTED_DIRECTIVES[d]})", location=f"{loc}.format")
    sample = datetime(2024, 2, 29, 13, 4, 5, 678901, tzinfo=timezone.utc)
    try:
        if directives & _YEAR_DIRECTIVES:
            datetime.strptime(sample.strftime(fmt), fmt)
        else:
            datetime.strptime(sample.strftime(f"%Y {fmt}"), f"%Y {fmt}")
    except ValueError as exc:
        raise ConfigError(f"invalid strptime format: {exc}", location=f"{loc}.format") from None
    spec = TimestampSpec(group, fmt, None, None)
    if "utc_offset" in value:
        if spec.has_zone:
            raise ConfigError("must be omitted: the format reads the offset from the timestamp (%z)",
                              location=f"{loc}.utc_offset")
        spec = TimestampSpec(group, fmt, check_utc_offset(value["utc_offset"], f"{loc}.utc_offset"), None)
    if "year" in value:
        if spec.has_year:
            raise ConfigError("must be omitted: the format already has a year", location=f"{loc}.year")
        spec = TimestampSpec(group, fmt, spec.utc_offset, check_year(value["year"], f"{loc}.year"))
    return spec


def _rule(raw: Any, loc: str, ts_group: str, default_subsystem: str, allowed: set[str]) -> Rule:
    if not isinstance(raw, dict):
        raise ConfigError("must be a mapping with name, match and event_type", location=loc)
    _keys(raw, _RULE_KEYS, loc)
    name = _str(raw.get("name"), f"{loc}.name")
    if not _NAME_RE.match(name):
        raise ConfigError("must be lowercase letters, digits, '_' or '-'", location=f"{loc}.name")
    pattern = _regex(raw.get("match"), f"{loc}.match")
    if ts_group not in pattern.groupindex:
        raise ConfigError(f"has no named group {ts_group!r} for the timestamp", location=f"{loc}.match")
    event_type = _str(raw.get("event_type"), f"{loc}.event_type")
    _probe(f"{loc}.event_type", event_type=event_type)
    subsystem = _subsystem(raw["subsystem"], allowed, f"{loc}.subsystem") if "subsystem" in raw else default_subsystem
    severity_text = raw.get("severity", "info")
    try:
        severity = Severity(severity_text)
    except ValueError:
        raise ConfigError(f"must be one of {[s.value for s in Severity]}", location=f"{loc}.severity") from None
    description = raw.get("description") or ""
    if not isinstance(description, str):
        raise ConfigError("must be a string", location=f"{loc}.description")

    attributes = _fields(raw.get("attributes", {}), pattern, ATTRIBUTE_TYPES, f"{loc}.attributes")
    for key in attributes:
        if key == SOURCE_ATTRIBUTE:
            raise ConfigError(f"{SOURCE_ATTRIBUTE!r} is reserved: it is set to the profile name",
                              location=f"{loc}.attributes.{key}")
        _probe(f"{loc}.attributes.{key}", attributes={key: 0})
    correlation = _fields(raw.get("correlation", {}), pattern, ID_TYPES, f"{loc}.correlation")
    for key in correlation:
        _probe(f"{loc}.correlation.{key}", correlation_ids={key: "0"})
    return Rule(name, pattern, event_type, subsystem, severity, attributes, correlation, description.strip())


def _fields(value: Any, pattern: re.Pattern[str], types: tuple[str, ...], loc: str) -> dict[str, Field]:
    if not isinstance(value, dict):
        raise ConfigError("must be a mapping of name to group or {group, type}", location=loc)
    out: dict[str, Field] = {}
    for key, spec in value.items():
        floc = f"{loc}.{key}"
        if not isinstance(key, str):
            raise ConfigError("names must be strings", location=floc)
        if isinstance(spec, str):
            spec = {"group": spec}
        if not isinstance(spec, dict):
            raise ConfigError("must be a group name or {group, type}", location=floc)
        _keys(spec, _FIELD_KEYS, floc)
        group = _str(spec.get("group"), f"{floc}.group")
        if group not in pattern.groupindex:
            raise ConfigError(f"names group {group!r}, which the rule's match does not define", location=f"{floc}.group")
        kind = spec.get("type", "str")
        if kind not in types:
            raise ConfigError(f"must be one of {list(types)}", location=f"{floc}.type")
        out[key] = Field(group, kind)
    return out


def _probe(loc: str, **fields: Any) -> None:
    """Check names against the event model's own rules, so a profile can never
    produce an event the core would reject."""
    base: dict[str, Any] = {"run_id": "profile", "event_id": "profile", "source_system": "profile",
                            "subsystem": "profile", "timestamp_ns": 0, "event_type": "profile.check"}
    try:
        NormalizedEvent(**{**base, **fields})
    except ValueError as exc:
        raise ConfigError(re.sub(r"^event\.", "", str(exc)), location=loc) from None


def _subsystem(value: Any, allowed: set[str], loc: str) -> str:
    text = _str(value, loc)
    if not _SUBSYSTEM_RE.match(text):
        raise ConfigError("must be lowercase letters, digits, '_' or '-'", location=loc)
    if allowed and text not in allowed:
        raise ConfigError(f"unknown subsystem {text!r}: a profile outside the built-in set may only use the "
                          f"adapter's subsystems {sorted(allowed)}", location=loc)
    return text


def _regex(value: Any, loc: str) -> re.Pattern[str]:
    text = _str(value, loc)
    try:
        return re.compile(text)
    except re.error as exc:
        raise ConfigError(f"invalid regex: {exc}", location=loc) from None


def _json_safe(value: dict[Any, Any], loc: str) -> dict[str, Any]:
    """The value as it will appear in the bundle's JSON (keys become strings)."""
    try:
        result: dict[str, Any] = json.loads(json.dumps(value, allow_nan=False, sort_keys=True))
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"must hold only strings, numbers, booleans, lists and mappings ({exc})",
                          location=loc) from None
    return result


def _keys(doc: Mapping[Any, Any], allowed: set[str], loc: str) -> None:
    unknown = set(doc) - allowed
    if unknown:
        raise ConfigError(f"unknown keys {sorted(map(str, unknown))} (allowed: {sorted(allowed)})", location=loc)


def _str(value: Any, loc: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigError("must be a non-empty string", location=loc)
    return value


def _list(value: Any, loc: str) -> list[Any]:
    if not isinstance(value, list):
        raise ConfigError("must be a list", location=loc)
    return value
