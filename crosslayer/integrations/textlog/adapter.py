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

* collect:   copy each input into ``raw_artifacts/`` under its own file name
* normalize: match each line against its profile's rules (first match wins),
             convert the timestamp to UTC, subtract the source's known clock
             offset, and time-order the events of all sources together.
             Unmatched lines and failed conversions become diagnostics.

Sources are validated at plan time (``faults_for``), before anything runs;
the runner does not pass ``inputs`` to plan-time hooks, so the check that
every input has exactly one source runs at the start of ``collect``.
"""

from __future__ import annotations

import codecs
import hashlib
from dataclasses import dataclass
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

_PARAMETERS = {"sources"}
_SOURCE_KEYS = {"input", "profile", "offset_ms", "utc_offset", "year"}


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
    return resolved


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
                notes.append(f"{profile.name}/{rule.name}")
        return [
            EventSpec(event_type, tuple(sorted(subs)), tuple(sorted(attrs)), tuple(sorted(ids)),
                      description="from " + ", ".join(notes))
            for event_type, (subs, attrs, ids, notes) in sorted(merged.items())
        ]

    def validate_parameters(self, parameters: Mapping[str, Any]) -> None:
        parse_sources(parameters)

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
        return NormalizationResult(
            events=order_events(events),
            diagnostics=diagnostics,
            stats={"sources": per_source, "events": len(events), "diagnostics": len(diagnostics)},
        )


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
