"""Adapter for line-oriented text logs, read through declarative log profiles.

Analyze mode only. An experiment lists its log files in ``inputs`` and gives
one ``parameters.sources`` entry per input, naming the profile that reads it
plus optional facts about that capture::

    inputs: [logs/client.log, logs/server.log]
    parameters:
      sources:
        - {input: logs/client.log, profile: synthetic_client}
        - {input: logs/server.log, profile: synthetic_server,
           utc_offset: "+01:00", year: 2026, offset_ms: 250}
      joins:
        - {name: ngap_ue, keys: [ran_ue_ngap_id, amf_ue_ngap_id],
           fallback_window_ms: 250}

* collect:   copy each input into ``raw_artifacts/`` under its own file name
* normalize: match each line against its profile's rules (first match wins),
             convert the timestamp to UTC, subtract the source's known clock
             offset, and time-order the events of all sources together.
             Configured joins add a composite correlation id from exact id
             pairs, or from an unambiguous bounded time-window fallback.
             Unmatched lines and failed conversions become diagnostics.

Sources are validated at plan time (``faults_for``), before anything runs;
the runner does not pass ``inputs`` to plan-time hooks, so the check that
every input has exactly one source runs at the start of ``collect``.
"""

from __future__ import annotations

import codecs
import hashlib
import json
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping, Sequence

from crosslayer.core.adapters.base import (
    AdapterDescription,
    Artifact,
    Capability,
    EventSpec,
    ExecutionResult,
    FaultSpec,
    NormalizationResult,
    ParseDiagnostic,
    RunContext,
    SystemAdapter,
)
from crosslayer.core.errors import ConfigError
from crosslayer.core.telemetry.events import NormalizedEvent
from crosslayer.core.telemetry.store import order_events
from crosslayer.core.units import ms_to_ns
from crosslayer.integrations.textlog.profile import (
    SOURCE_ATTRIBUTE,
    LogProfile,
    TimestampParser,
    builtin_profiles,
    check_utc_offset,
    check_year,
    load_profile,
    timestamp_parser,
)

_PARAMETERS = {"sources", "joins"}
_SOURCE_KEYS = {"input", "profile", "offset_ms", "utc_offset", "year"}
_JOIN_KEYS = {"name", "keys", "fallback_window_ms"}
_CORRELATION_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True)
class Source:
    """One ``parameters.sources`` entry, checked for shape only."""

    index: int
    input: str
    profile: str
    offset_ms: int | float = 0
    utc_offset: str | None = None
    year: int | None = None

    @property
    def artifact(self) -> str:
        return Path(self.input).name

    @property
    def loc(self) -> str:
        return f"parameters.sources[{self.index}]"


@dataclass(frozen=True)
class ResolvedSource:
    source: Source
    path: Path
    profile: LogProfile
    parser: TimestampParser
    offset_ns: int

    def confidence(self) -> dict[str, Any]:
        meta: dict[str, Any] = {
            "timestamp_source": "log_text",
            "utc_offset": self.parser.utc_offset or "in_timestamp",
            "offset_ms": self.source.offset_ms,
            "clock": dict(self.profile.clock),
        }
        if self.parser.year is not None:
            meta["assumed_year"] = self.parser.year
        return meta


@dataclass(frozen=True)
class Join:
    """A composite correlation id derived from explicit ids or a bounded time fallback."""

    index: int
    name: str
    keys: tuple[str, str]
    fallback_window_ms: int | float
    fallback_window_ns: int

    @property
    def loc(self) -> str:
        return f"parameters.joins[{self.index}]"


def parse_sources(parameters: Mapping[str, Any]) -> list[Source]:
    """Check the shape of ``parameters`` (no file access) and return its sources."""
    unknown = set(parameters) - _PARAMETERS
    if unknown:
        raise ConfigError(f"unknown parameters {sorted(unknown)} (allowed: {sorted(_PARAMETERS)})",
                          location="parameters")
    raw = parameters.get("sources")
    if not isinstance(raw, list) or not raw:
        raise ConfigError("must be a non-empty list of {input, profile} entries, one per input",
                          location="parameters.sources")
    sources: list[Source] = []
    for i, entry in enumerate(raw):
        loc = f"parameters.sources[{i}]"
        if not isinstance(entry, Mapping):
            raise ConfigError("must be a mapping with input and profile", location=loc)
        extra = set(entry) - _SOURCE_KEYS
        if extra:
            raise ConfigError(f"unknown keys {sorted(map(str, extra))} (allowed: {sorted(_SOURCE_KEYS)})",
                              location=loc)
        for key in ("input", "profile"):
            if not isinstance(entry.get(key), str) or not entry[key]:
                raise ConfigError("must be a non-empty string", location=f"{loc}.{key}")
        offset = entry.get("offset_ms", 0)
        if isinstance(offset, bool) or not isinstance(offset, (int, float)):
            raise ConfigError("must be a number of milliseconds", location=f"{loc}.offset_ms")
        try:
            ms_to_ns(offset)
        except ValueError as exc:
            raise ConfigError(str(exc), location=f"{loc}.offset_ms") from None
        utc_offset = check_utc_offset(entry["utc_offset"], f"{loc}.utc_offset") if "utc_offset" in entry else None
        year = check_year(entry["year"], f"{loc}.year") if "year" in entry else None
        source = Source(i, entry["input"], entry["profile"], offset, utc_offset, year)
        for other in sources:
            if other.input == source.input:
                raise ConfigError(f"input {source.input!r} already has a source ({other.loc})", location=f"{loc}.input")
        sources.append(source)
    return sources


def parse_joins(parameters: Mapping[str, Any]) -> list[Join]:
    """Validate configured composite-id joins without reading profiles or files."""
    raw = parameters.get("joins", [])
    if not isinstance(raw, list):
        raise ConfigError("must be a list of join mappings", location="parameters.joins")
    joins: list[Join] = []
    for i, entry in enumerate(raw):
        loc = f"parameters.joins[{i}]"
        if not isinstance(entry, Mapping):
            raise ConfigError("must be a mapping with name, keys and fallback_window_ms", location=loc)
        extra = set(entry) - _JOIN_KEYS
        if extra:
            raise ConfigError(f"unknown keys {sorted(map(str, extra))} (allowed: {sorted(_JOIN_KEYS)})", location=loc)
        missing = _JOIN_KEYS - set(entry)
        if missing:
            raise ConfigError(f"missing required keys {sorted(missing)}", location=loc)
        name = entry["name"]
        if not isinstance(name, str) or not _CORRELATION_NAME_RE.match(name):
            raise ConfigError("must be a lowercase snake_case correlation id name", location=f"{loc}.name")
        if any(join.name == name for join in joins):
            raise ConfigError(f"duplicate join name {name!r}", location=f"{loc}.name")
        keys = entry["keys"]
        if (not isinstance(keys, list) or len(keys) != 2 or len(set(keys)) != 2
                or not all(isinstance(key, str) and _CORRELATION_NAME_RE.match(key) for key in keys)):
            raise ConfigError("must be two distinct lowercase snake_case correlation id names", location=f"{loc}.keys")
        window = entry["fallback_window_ms"]
        if isinstance(window, bool) or not isinstance(window, (int, float)) or window <= 0:
            raise ConfigError("must be a positive number of milliseconds", location=f"{loc}.fallback_window_ms")
        try:
            window_ns = ms_to_ns(window)
        except ValueError as exc:
            raise ConfigError(str(exc), location=f"{loc}.fallback_window_ms") from None
        joins.append(Join(i, name, (keys[0], keys[1]), window, window_ns))
    return joins


def builtin_subsystems() -> set[str]:
    return {s for p in _builtins() for s in p.subsystems}


def resolve_sources(parameters: Mapping[str, Any], base_dir: Path) -> list[ResolvedSource]:
    """Load every source's profile and check its file, or raise ``ConfigError``."""
    declared = builtin_subsystems()
    resolved: list[ResolvedSource] = []
    for source in parse_sources(parameters):
        path = (base_dir / source.input).resolve()
        if not path.is_file():
            raise ConfigError(f"file not found: {path}", location=f"{source.loc}.input")
        for other in resolved:
            if other.path == path:
                raise ConfigError(f"same file as {other.source.loc}", location=f"{source.loc}.input")
            if other.source.artifact == source.artifact:
                raise ConfigError(f"file name {source.artifact!r} is also used by {other.source.loc}; "
                                  "raw artifacts are stored by file name, so rename one", location=f"{source.loc}.input")
        profile = load_profile(source.profile, base_dir, subsystems=declared, loc=f"{source.loc}.profile")
        parser = timestamp_parser(profile, source.utc_offset, source.year, source.loc)
        resolved.append(ResolvedSource(source, path, profile, parser, ms_to_ns(source.offset_ms)))
    _validate_joins(parse_joins(parameters), resolved)
    return resolved


def _validate_joins(joins: Sequence[Join], sources: Sequence[ResolvedSource]) -> None:
    for join in joins:
        profiles = [source.profile for source in sources]
        declared = [profile.name for profile in profiles if join.name in profile.derived_correlation]
        if not declared:
            raise ConfigError(
                f"no selected profile declares derived correlation id {join.name!r}", location=f"{join.loc}.name"
            )
        emitted = {key for profile in profiles for rule in profile.rules for key in rule.correlation}
        for i, key in enumerate(join.keys):
            if key not in emitted:
                raise ConfigError(f"no selected profile emits correlation id {key!r}", location=f"{join.loc}.keys[{i}]")
        if not any(set(join.keys) <= set(rule.correlation) for profile in profiles for rule in profile.rules):
            raise ConfigError("no selected profile rule emits both join keys", location=f"{join.loc}.keys")
        if join.name in emitted:
            raise ConfigError(
                f"selected profiles already emit correlation id {join.name!r}", location=f"{join.loc}.name"
            )


def _builtins() -> list[LogProfile]:
    return [load_profile(name) for name in builtin_profiles()]


class TextLogAdapter(SystemAdapter):
    def describe(self) -> AdapterDescription:
        profiles = _builtins()
        digest = hashlib.sha256("".join(f"{p.name}:{p.sha256};" for p in profiles).encode()).hexdigest()[:12]
        return AdapterDescription(
            name="textlog",
            adapter_version="0.1.0",
            system="line-oriented text logs read through declarative log profiles",
            system_version=f"built-in profiles sha256:{digest}",
            subsystems=tuple(sorted(builtin_subsystems())),
            description=(
                "Analyze mode only: turns existing text logs into events with YAML log profiles "
                "(built-in: " + ", ".join(p.name for p in profiles) + "), one profile per input file."
            ),
        )

    def capabilities(self) -> frozenset[Capability]:
        return frozenset({Capability.COLLECT, Capability.NORMALIZE})

    def event_catalog(self) -> list[EventSpec]:
        """Every event the built-in profiles can emit. Profiles loaded from a
        path are not known here, so lint cannot check contracts against them."""
        merged: dict[str, tuple[set[str], set[str], set[str], list[str]]] = {}
        for profile in _builtins():
            for rule in profile.rules:
                subsystems, attributes, ids, notes = merged.setdefault(
                    rule.event_type, (set(), {SOURCE_ATTRIBUTE}, set(), []))
                subsystems.add(rule.subsystem)
                attributes.update(rule.attributes)
                ids.update(rule.correlation)
                ids.update(profile.derived_correlation)
                notes.append(f"{profile.name}/{rule.name}")
        return [
            EventSpec(event_type, tuple(sorted(subs)), tuple(sorted(attrs)), tuple(sorted(ids)),
                      description="from " + ", ".join(notes))
            for event_type, (subs, attrs, ids, notes) in sorted(merged.items())
        ]

    def validate_parameters(self, parameters: Mapping[str, Any]) -> None:
        parse_sources(parameters)
        parse_joins(parameters)

    def faults_for(self, parameters: Mapping[str, Any], base_dir: Path) -> list[FaultSpec]:
        """No faults; called at plan time, so it also loads every profile and checks every file."""
        resolve_sources(parameters, base_dir)
        return []

    def collect(self, ctx: RunContext, execution: ExecutionResult | None) -> list[Artifact]:
        resolved = resolve_sources(ctx.parameters, ctx.base_dir)
        by_path = {r.path: r for r in resolved}
        seen: set[Path] = set()
        for given in ctx.inputs:
            path = Path(given).resolve()
            if path in seen:
                raise ConfigError(f"{path} is listed twice", location="inputs")
            seen.add(path)
            if path not in by_path:
                raise ConfigError(f"input {path.name!r} has no entry in parameters.sources",
                                  location="parameters.sources")
        for r in resolved:
            if r.path not in seen:
                raise ConfigError(f"{r.source.input!r} is not listed in inputs", location=f"{r.source.loc}.input")

        artifacts = []
        for r in resolved:
            target = ctx.raw_dir / r.source.artifact
            target.write_bytes(r.path.read_bytes())
            artifacts.append(Artifact(r.source.artifact, target,
                                      description=f"{r.source.input} read with profile {r.profile.name}"))
        return artifacts

    def normalize(self, ctx: RunContext, artifacts: Sequence[Artifact]) -> NormalizationResult:
        by_name = {a.name: a for a in artifacts}
        events: list[NormalizedEvent] = []
        diagnostics: list[ParseDiagnostic] = []
        per_source = [
            _read_source(ctx, r, by_name[r.source.artifact].path, events, diagnostics)
            for r in resolve_sources(ctx.parameters, ctx.base_dir)
        ]
        ordered = order_events(events)
        joined, join_stats = _apply_joins(ordered, parse_joins(ctx.parameters))
        return NormalizationResult(
            events=joined,
            diagnostics=diagnostics,
            stats={"sources": per_source, "joins": join_stats, "events": len(events), "diagnostics": len(diagnostics)},
        )


def _apply_joins(events: Sequence[NormalizedEvent], joins: Sequence[Join]) -> tuple[list[NormalizedEvent], list[dict[str, Any]]]:
    """Add configured composite correlation ids without guessing across ambiguous windows."""
    joined = list(events)
    stats: list[dict[str, Any]] = []
    for join in joins:
        anchors: list[tuple[NormalizedEvent, dict[str, str], str]] = []
        for event in joined:
            if all(key in event.correlation_ids for key in join.keys):
                values = {key: event.correlation_ids[key] for key in join.keys}
                composite = json.dumps([values[key] for key in join.keys], separators=(",", ":"))
                anchors.append((event, values, composite))

        counts = {"exact": 0, "fallback": 0, "ambiguous": 0, "unmatched": 0}
        output: list[NormalizedEvent] = []
        for event in joined:
            present = {key: event.correlation_ids[key] for key in join.keys if key in event.correlation_ids}
            if len(present) == len(join.keys):
                values = {key: present[key] for key in join.keys}
                composite = json.dumps([values[key] for key in join.keys], separators=(",", ":"))
                output.append(_joined_event(event, join, composite, {
                    "method": "id_pair", "confidence": "high", "ids": values,
                }))
                counts["exact"] += 1
                continue

            candidates = [
                anchor for anchor in anchors
                if abs(anchor[0].timestamp_ns - event.timestamp_ns) <= join.fallback_window_ns
                and all(anchor[1][key] == value for key, value in present.items())
            ]
            composites = {anchor[2] for anchor in candidates}
            if len(composites) == 1:
                anchor = min(candidates, key=lambda item: (
                    abs(item[0].timestamp_ns - event.timestamp_ns), item[0].timestamp_ns, item[0].event_id,
                ))
                delta_ns = abs(anchor[0].timestamp_ns - event.timestamp_ns)
                output.append(_joined_event(event, join, anchor[2], {
                    "method": "time_window",
                    "confidence": "low",
                    "ids": anchor[1],
                    "window_ms": join.fallback_window_ms,
                    "delta_ms": delta_ns / 1_000_000,
                    "anchor_event_id": anchor[0].event_id,
                }))
                counts["fallback"] += 1
            else:
                output.append(event)
                key = "ambiguous" if len(composites) > 1 else "unmatched"
                counts[key] += 1
        joined = output
        stats.append({
            "name": join.name,
            "keys": list(join.keys),
            "fallback_window_ms": join.fallback_window_ms,
            **counts,
        })
    return joined, stats


def _joined_event(event: NormalizedEvent, join: Join, value: str, metadata: Mapping[str, Any]) -> NormalizedEvent:
    ids = {**event.correlation_ids, join.name: value}
    joins = {**event.confidence_metadata.get("correlation_joins", {}), join.name: dict(metadata)}
    confidence = {**event.confidence_metadata, "correlation_joins": joins}
    return replace(event, correlation_ids=ids, confidence_metadata=confidence)


def _read_source(
    ctx: RunContext, src: ResolvedSource, path: Path,
    events: list[NormalizedEvent], diagnostics: list[ParseDiagnostic],
) -> dict[str, Any]:
    """Parse one log file into ``events``; problems go to ``diagnostics``. Returns stats."""
    profile, name = src.profile, src.source.artifact
    confidence = src.confidence()
    stats: dict[str, Any] = {
        "input": src.source.input, "artifact": name, "profile": profile.name, "builtin": profile.builtin,
        "profile_sha256": profile.sha256, "offset_ms": src.source.offset_ms,
        "lines": 0, "blank": 0, "skipped": 0, "unmatched": 0, "events": 0, "diagnostics": 0,
    }

    def problem(line: int, message: str) -> None:
        diagnostics.append(ParseDiagnostic(name, line, message))
        stats["diagnostics"] += 1

    with path.open("rb") as fh:
        for lineno, raw in enumerate(fh, start=1):
            stats["lines"] += 1
            if lineno == 1:
                raw = raw.removeprefix(codecs.BOM_UTF8)
            try:
                line = raw.decode("utf-8").rstrip("\r\n")
            except UnicodeDecodeError as exc:
                problem(lineno, f"not valid UTF-8 ({exc.reason} at byte {exc.start})")
                continue
            if not line.strip():
                stats["blank"] += 1
                continue
            if profile.skips(line):
                stats["skipped"] += 1
                continue
            parsed = profile.match(line)
            if parsed is None:
                stats["unmatched"] += 1
                if profile.unmatched == "diagnostic":
                    problem(lineno, f"no rule in profile {profile.name!r} matches: {line[:120]!r}")
                continue
            if isinstance(parsed, str):
                problem(lineno, parsed)
                continue
            rule = parsed.rule
            try:
                timestamp = src.parser.parse(parsed.timestamp_text) - src.offset_ns
                if timestamp < 0:
                    raise ValueError("timestamp is before 1970-01-01T00:00:00Z after offset_ms")
                event = NormalizedEvent(
                    run_id=ctx.run_id,
                    event_id=f"{name}:{lineno}",
                    source_system=profile.source_system,
                    subsystem=rule.subsystem,
                    timestamp_ns=timestamp,
                    event_type=rule.event_type,
                    severity=rule.severity,
                    correlation_ids=parsed.correlation,
                    attributes={**parsed.attributes, SOURCE_ATTRIBUTE: profile.name},
                    raw_artifact_ref=ctx.raw_ref(name, lineno),
                    confidence_metadata=confidence,
                )
            except ValueError as exc:
                problem(lineno, f"rule {rule.name!r}: timestamp {parsed.timestamp_text!r}: {exc}")
                continue
            events.append(event)
            stats["events"] += 1
    return stats
