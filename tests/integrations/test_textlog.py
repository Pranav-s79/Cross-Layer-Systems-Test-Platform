"""textlog: declarative log profiles and multi-source normalization."""

from __future__ import annotations

import datetime as dt
import shutil
import warnings
from pathlib import Path
from typing import Any, Callable

import pytest
import yaml

from crosslayer.core.adapters import RunContext, check_adapter_conformance
from crosslayer.core.errors import ConfigError
from crosslayer.integrations.textlog import TextLogAdapter
from crosslayer.integrations.textlog.profile import (
    PROFILES_DIR,
    TimestampParser,
    builtin_profiles,
    load_profile,
)

UTC = dt.timezone.utc


def _ns(*args: int, us: int = 0) -> int:
    """Nanoseconds since the epoch for a UTC wall-clock time."""
    return int(dt.datetime(*args, tzinfo=UTC).timestamp()) * 1_000_000_000 + us * 1_000


def _profile_doc() -> dict[str, Any]:
    """A minimal valid profile for a custom (path) profile."""
    return {
        "schema_version": 1,
        "name": "custom",
        "source_system": "custom-app",
        "subsystem": "client",
        "timestamp": {"group": "ts", "format": "%Y-%m-%d %H:%M:%S.%f", "utc_offset": "+00:00"},
        "clock": {"source": "test clock"},
        "rules": [{
            "name": "hello",
            "match": r"^(?P<ts>\S+ \S+) hello id=(?P<id>\S+) n=(?P<n>\S+)(?: tag=(?P<tag>\w+))?$",
            "event_type": "app.hello",
            "attributes": {"app.n": {"group": "n", "type": "int"}, "app.tag": "tag"},
            "correlation": {"item": "id"},
        }],
    }


def _write_profile(directory: Path, doc: dict[str, Any], name: str = "custom.yaml") -> Path:
    path = directory / name
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return path


def _normalize(tmp_path: Path, sources: list[dict[str, Any]], files: dict[str, bytes | str]):
    """Validate, collect and normalize like the runner does, without a bundle."""
    base = tmp_path / "exp"
    base.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        data = content.encode("utf-8") if isinstance(content, str) else content
        (base / name).parent.mkdir(parents=True, exist_ok=True)
        (base / name).write_bytes(data)
    params = {"sources": sources}
    adapter = TextLogAdapter()
    adapter.validate_parameters(params)
    assert adapter.faults_for(params, base) == []
    raw = tmp_path / "raw"
    raw.mkdir()
    ctx = RunContext(run_id="t", seed=0, parameters=params, faults=(), raw_dir=raw, work_dir=tmp_path,
                     base_dir=base, timeout_s=10, inputs=tuple((base / s["input"]).resolve() for s in sources))
    return adapter.normalize(ctx, adapter.collect(ctx, None))


# -- adapter -----------------------------------------------------------------------------


def test_adapter_conforms_and_is_analyze_only():
    adapter = TextLogAdapter()
    assert check_adapter_conformance(adapter) == []
    assert {str(c) for c in adapter.capabilities()} == {"collect", "normalize"}
    assert adapter.faults() == [] and adapter.contracts() == []
    assert adapter.describe().subsystems == ("client", "server")


def test_builtin_profiles_load():
    assert builtin_profiles() == ["synthetic_client", "synthetic_server"]
    for name in builtin_profiles():
        profile = load_profile(name)
        assert profile.builtin and profile.name == name and len(profile.sha256) == 12


def test_builtin_profile_copied_to_a_path_loads_as_custom(tmp_path):
    shutil.copyfile(PROFILES_DIR / "synthetic_client.yaml", tmp_path / "mine.yaml")
    profile = load_profile("mine.yaml", tmp_path, subsystems={"client", "server"})
    assert not profile.builtin and profile.name == "synthetic_client"


# -- profile validation ------------------------------------------------------------------


def _set(path: str, value: Any) -> Callable[[dict[str, Any]], None]:
    """Change one dotted path in a profile document; None deletes it."""
    def change(doc: dict[str, Any]) -> None:
        *parents, last = path.split(".")
        target: Any = doc
        for key in parents:
            target = target[int(key)] if isinstance(target, list) else target[key]
        if value is None:
            del target[last]
        else:
            target[last] = value
    return change


def _rule(**fields: Any) -> Callable[[dict[str, Any]], None]:
    def change(doc: dict[str, Any]) -> None:
        doc["rules"][0].update(fields)
    return change


@pytest.mark.parametrize(
    "change, match",
    [
        (_set("surprise", 1), r"custom\.yaml: unknown keys \['surprise'\]"),
        (_set("name", None), r"custom\.yaml: name: is required"),
        (_set("schema_version", 2), r"schema_version: unsupported schema_version"),
        (_set("name", "My Profile"), r"name: must be lowercase"),
        (_set("source_system", ""), r"source_system: must be a non-empty string"),
        (_set("subsystem", "gpu"), r"subsystem: unknown subsystem 'gpu'"),
        (_set("timestamp", "iso"), r"timestamp: must be a mapping"),
        (_set("timestamp.zone", "UTC"), r"timestamp: unknown keys \['zone'\]"),
        (_set("timestamp.group", None), r"timestamp\.group: must be a non-empty string"),
        (_set("timestamp.format", "%Y-%m-%d %H:%M:%S %Z"), r"timestamp\.format: %Z is not supported"),
        (_set("timestamp.format", "%Y-%m-%d %Q"), r"timestamp\.format: invalid strptime format"),
        (_set("timestamp.format", "%H:%M:%S %"), r"timestamp\.format: ends with a lone '%'"),
        (_set("timestamp.year", 2026), r"timestamp\.year: must be omitted: the format already has a year"),
        (_set("timestamp.utc_offset", "UTC"), r"timestamp\.utc_offset: must be a fixed offset"),
        (_set("timestamp.utc_offset", "+24:00"), r"timestamp\.utc_offset: must be a fixed offset"),
        (_set("timestamp.format", "%Y-%m-%dT%H:%M:%S%z"), r"timestamp\.utc_offset: must be omitted"),
        (_set("clock", None), r"clock: is required"),
        (_set("clock", {}), r"clock: must be a non-empty mapping"),
        (_set("clock", {"synced": dt.date(2026, 1, 1)}), r"clock: must hold only strings"),
        (_set("skip", ["("]), r"skip\[0\]: invalid regex"),
        (_set("unmatched", "drop"), r"unmatched: must be one of \['diagnostic', 'ignore'\]"),
        (_set("rules", []), r"rules: must list at least one rule"),
        (_rule(when="always"), r"rules\[0\]: unknown keys \['when'\]"),
        (_rule(name="Hello"), r"rules\[0\]\.name: must be lowercase"),
        (_rule(match="(?P<ts>"), r"rules\[0\]\.match: invalid regex"),
        (_rule(match=r"^(?P<time>\S+) hello id=(?P<id>\d+) n=(?P<n>\d+)$"),
         r"rules\[0\]\.match: has no named group 'ts'"),
        (_rule(event_type="App Hello"), r"rules\[0\]\.event_type: event_type 'App Hello' must be dotted lowercase"),
        (_rule(subsystem="gpu"), r"rules\[0\]\.subsystem: unknown subsystem 'gpu'"),
        (_rule(severity="loud"), r"rules\[0\]\.severity: must be one of"),
        (_rule(attributes={"app.n": "nope"}), r"rules\[0\]\.attributes\.app\.n\.group: names group 'nope'"),
        (_rule(attributes={"app.n": {"group": "n", "type": "bool"}}),
         r"rules\[0\]\.attributes\.app\.n\.type: must be one of \['str', 'int', 'hex', 'float'\]"),
        (_rule(attributes={"app.n": {"group": "n", "unit": "ms"}}), r"rules\[0\]\.attributes\.app\.n: unknown keys"),
        (_rule(attributes={"count": "n"}), r"rules\[0\]\.attributes\.count: attribute key 'count' must be namespaced"),
        (_rule(attributes={"log.source": "n"}), r"rules\[0\]\.attributes\.log\.source: 'log\.source' is reserved"),
        (_rule(correlation={"Item": "id"}), r"rules\[0\]\.correlation\.Item: correlation id key 'Item'"),
        (_rule(correlation={"item": {"group": "id", "type": "float"}}),
         r"rules\[0\]\.correlation\.item\.type: must be one of \['str', 'int', 'hex'\]"),
        (_rule(correlation={"item": "missing"}), r"rules\[0\]\.correlation\.item\.group: names group 'missing'"),
        (lambda doc: doc["rules"].append(dict(doc["rules"][0])), r"rules\[1\]\.name: duplicate rule name 'hello'"),
    ],
)
def test_invalid_profiles_name_the_offending_field(tmp_path, change, match):
    doc = _profile_doc()
    change(doc)
    _write_profile(tmp_path, doc)
    (tmp_path / "app.log").write_text("", encoding="utf-8")
    with pytest.raises(ConfigError, match=match):
        TextLogAdapter().faults_for({"sources": [{"input": "app.log", "profile": "custom.yaml"}]}, tmp_path)


def test_year_less_profile_needs_a_year_somewhere(tmp_path):
    doc = _profile_doc()
    doc["timestamp"]["format"] = "%b %d %H:%M:%S"
    _write_profile(tmp_path, doc)
    (tmp_path / "app.log").write_text("", encoding="utf-8")
    source = {"input": "app.log", "profile": "custom.yaml"}
    with pytest.raises(ConfigError, match=r"parameters\.sources\[0\]\.year: is required"):
        TextLogAdapter().faults_for({"sources": [source]}, tmp_path)
    TextLogAdapter().faults_for({"sources": [{**source, "year": 2026}]}, tmp_path)
    doc["timestamp"]["year"] = 1800
    _write_profile(tmp_path, doc)
    with pytest.raises(ConfigError, match=r"timestamp\.year: must be a year between 1970 and 9999"):
        TextLogAdapter().faults_for({"sources": [source]}, tmp_path)


# -- source parameters -------------------------------------------------------------------


def _client(**extra: Any) -> dict[str, Any]:
    return {"input": "a.log", "profile": "synthetic_client", **extra}


def _server(**extra: Any) -> dict[str, Any]:
    return {"input": "b.log", "profile": "synthetic_server", "utc_offset": "+01:00", "year": 2026, **extra}


@pytest.mark.parametrize(
    "parameters, match",
    [
        ({}, r"parameters\.sources: must be a non-empty list"),
        ({"sources": []}, r"parameters\.sources: must be a non-empty list"),
        ({"sources": [_client()], "profiles": []}, r"parameters: unknown parameters \['profiles'\]"),
        ({"sources": ["a.log"]}, r"parameters\.sources\[0\]: must be a mapping"),
        ({"sources": [_client(offset=1)]}, r"parameters\.sources\[0\]: unknown keys \['offset'\]"),
        ({"sources": [{"profile": "synthetic_client"}]}, r"parameters\.sources\[0\]\.input: must be a non-empty"),
        ({"sources": [_client(profile=3)]}, r"parameters\.sources\[0\]\.profile: must be a non-empty"),
        ({"sources": [_client(offset_ms="250")]}, r"parameters\.sources\[0\]\.offset_ms: must be a number"),
        ({"sources": [_client(offset_ms=True)]}, r"parameters\.sources\[0\]\.offset_ms: must be a number"),
        ({"sources": [_client(offset_ms=1e-7)]}, r"parameters\.sources\[0\]\.offset_ms: .*whole number"),
        ({"sources": [_client(utc_offset="+1")]}, r"parameters\.sources\[0\]\.utc_offset: must be a fixed offset"),
        ({"sources": [_server(year="2026")]}, r"parameters\.sources\[0\]\.year: must be a year"),
        ({"sources": [_client(), _client()]}, r"parameters\.sources\[1\]\.input: input 'a\.log' already has a source"),
        ({"sources": [_client(profile="nope")]}, r"parameters\.sources\[0\]\.profile: unknown profile 'nope'"),
        ({"sources": [_client(input="missing.log")]}, r"parameters\.sources\[0\]\.input: file not found"),
        ({"sources": [_client(input="x/app.log"), _client(input="y/app.log")]},
         r"parameters\.sources\[1\]\.input: file name 'app\.log' is also used"),
        ({"sources": [_client(), _client(input="./a.log")]}, r"parameters\.sources\[1\]\.input: same file as"),
        ({"sources": [_server(year=None)]}, r"parameters\.sources\[0\]\.year: is required"),
        ({"sources": [_server(utc_offset=None)]}, r"parameters\.sources\[0\]\.utc_offset: is required"),
        ({"sources": [_client(year=2026)]},
         r"parameters\.sources\[0\]\.year: profile 'synthetic_client' reads the year from the timestamp"),
    ],
)
def test_invalid_sources_name_the_offending_field(tmp_path, parameters, match):
    for name in ("a.log", "b.log", "x/app.log", "y/app.log"):
        (tmp_path / name).parent.mkdir(exist_ok=True)
        (tmp_path / name).write_text("", encoding="utf-8")
    for source in parameters.get("sources", []):
        if isinstance(source, dict):
            for key in [k for k, v in source.items() if v is None]:
                del source[key]
    adapter = TextLogAdapter()
    with pytest.raises(ConfigError, match=match):
        adapter.validate_parameters(parameters)
        adapter.faults_for(parameters, tmp_path)


# -- timestamps --------------------------------------------------------------------------


def test_timestamps_apply_the_utc_offset_exactly():
    iso = TimestampParser("%Y-%m-%d %H:%M:%S.%f", "+02:00", None)
    assert iso.parse("2026-11-17 11:00:00.250") == _ns(2026, 11, 17, 9, 0, 0, us=250_000)
    assert iso.parse("2026-11-17 11:00:00.000001") - iso.parse("2026-11-17 11:00:00.0") == 1_000
    west = TimestampParser("%Y-%m-%d %H:%M:%S", "-05:30", None)
    assert west.parse("2026-11-17 03:30:00") == _ns(2026, 11, 17, 9, 0, 0)
    zoned = TimestampParser("%Y-%m-%dT%H:%M:%S.%f%z", None, None)
    assert zoned.parse("2026-11-17T10:00:00.5+01:00") == _ns(2026, 11, 17, 9, 0, 0, us=500_000)
    assert zoned.parse("2026-11-17T09:00:00.5Z") == _ns(2026, 11, 17, 9, 0, 0, us=500_000)


def test_year_less_timestamps_use_the_configured_year():
    syslog = TimestampParser("%b %d %H:%M:%S.%f", "+00:00", 2024)
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # Python 3.13+ warns about year-less strptime formats
        assert syslog.parse("Feb 29 12:00:00.5") == _ns(2024, 2, 29, 12, 0, 0, us=500_000)
        assert syslog.parse("Nov  7 08:00:00.0") == _ns(2024, 11, 7, 8, 0, 0)


def test_bad_or_pre_epoch_timestamps_raise_value_error():
    parser = TimestampParser("%Y-%m-%d %H:%M:%S", "+00:00", None)
    with pytest.raises(ValueError):
        parser.parse("2026-13-45 00:00:00")
    with pytest.raises(ValueError, match="before 1970"):
        parser.parse("1969-12-31 23:59:59")


def test_offset_ms_and_source_overrides_shift_timestamps(tmp_path):
    doc = _profile_doc()
    _write_profile(tmp_path, doc)
    line = "2026-11-17 09:00:00.000 hello id=1 n=5\n"
    result = _normalize(tmp_path / "run", [
        {"input": "a.log", "profile": str(tmp_path / "custom.yaml"), "offset_ms": 250.5},
        {"input": "b.log", "profile": str(tmp_path / "custom.yaml"), "utc_offset": "+01:00", "offset_ms": -1000},
    ], {"a.log": line, "b.log": line})
    by_id = {e.event_id: e for e in result.events}
    assert by_id["a.log:1"].timestamp_ns == _ns(2026, 11, 17, 9, 0, 0) - 250_500_000
    assert by_id["b.log:1"].timestamp_ns == _ns(2026, 11, 17, 8, 0, 0) + 1_000_000_000
    assert by_id["b.log:1"].confidence_metadata == {
        "timestamp_source": "log_text", "utc_offset": "+01:00", "offset_ms": -1000, "clock": {"source": "test clock"}}


# -- line matching -----------------------------------------------------------------------


def test_first_matching_rule_wins(tmp_path):
    doc = _profile_doc()
    special = dict(doc["rules"][0], name="hello_zero", event_type="app.hello_zero", severity="warning",
                   match=r"^(?P<ts>\S+ \S+) hello id=(?P<id>\S+) n=(?P<n>0)$", attributes={})
    doc["rules"].insert(0, special)
    _write_profile(tmp_path, doc)
    result = _normalize(tmp_path / "run", [{"input": "a.log", "profile": str(tmp_path / "custom.yaml")}], {
        "a.log": "2026-11-17 09:00:00.0 hello id=1 n=0\n2026-11-17 09:00:01.0 hello id=2 n=7\n"})
    assert [(e.event_type, str(e.severity)) for e in result.events] == [
        ("app.hello_zero", "warning"), ("app.hello", "info")]
    assert result.diagnostics == []


def test_values_are_typed_and_optional_groups_are_omitted(tmp_path):
    doc = _profile_doc()
    doc["rules"][0]["correlation"] = {"item": {"group": "id", "type": "hex"}}
    _write_profile(tmp_path, doc)
    result = _normalize(tmp_path / "run", [{"input": "a.log", "profile": str(tmp_path / "custom.yaml")}], {
        "a.log": "2026-11-17 09:00:00.0 hello id=0x1F n=-3 tag=x\n2026-11-17 09:00:01.0 hello id=1f n=4\n"})
    first, second = result.events
    assert first.attributes == {"app.n": -3, "app.tag": "x", "log.source": "custom"}
    assert first.correlation_ids == {"item": "31"} == second.correlation_ids  # hex ids join across spellings
    assert "app.tag" not in second.attributes


def test_conversion_failures_become_diagnostics(tmp_path):
    doc = _profile_doc()
    doc["rules"][0]["correlation"] = {"item": {"group": "id", "type": "int"}}
    _write_profile(tmp_path, doc)
    result = _normalize(tmp_path / "run", [{"input": "a.log", "profile": str(tmp_path / "custom.yaml")}], {
        "a.log": "\n".join([
            "2026-11-17 09:00:00.0 hello id=1 n=five",
            "2026-11-17 09:00:01.0 hello id=one n=5",
            "2026-13-45 09:00:02.0 hello id=3 n=5",
            "2026-11-17 09:00:03.0 hello id=4 n=5",
        ]) + "\n"})
    assert [e.event_id for e in result.events] == ["a.log:4"]
    assert [(d.artifact, d.line) for d in result.diagnostics] == [("a.log", 1), ("a.log", 2), ("a.log", 3)]
    assert "attribute 'app.n' = 'five' is not int" in result.diagnostics[0].message
    assert "correlation id 'item' = 'one' is not int" in result.diagnostics[1].message
    assert "timestamp '2026-13-45 09:00:02.0'" in result.diagnostics[2].message


@pytest.mark.parametrize("unmatched, diagnostics", [("diagnostic", 2), ("ignore", 1)])
def test_unmatched_lines_are_diagnostics_unless_ignored(tmp_path, unmatched, diagnostics):
    doc = _profile_doc()
    doc["unmatched"] = unmatched
    doc["skip"] = [r"^#"]
    _write_profile(tmp_path, doc)
    content = (b"\xef\xbb\xbf2026-11-17 09:00:00.0 hello id=1 n=1\n"   # UTF-8 byte order mark on line 1
               b"# a comment\n\n"
               b"something else entirely\n"
               b"2026-11-17 09:00:01.0 hello id=\xff n=2\n"           # not UTF-8: always a diagnostic
               b"2026-11-17 09:00:02.0 hello id=3 n=3\r\n")
    result = _normalize(tmp_path / "run", [{"input": "a.log", "profile": str(tmp_path / "custom.yaml")}],
                        {"a.log": content})
    assert [e.event_id for e in result.events] == ["a.log:1", "a.log:6"]
    assert len(result.diagnostics) == diagnostics
    assert "not valid UTF-8" in result.diagnostics[-1].message and result.diagnostics[-1].line == 5
    [stats] = result.stats["sources"]
    assert (stats["lines"], stats["blank"], stats["skipped"], stats["unmatched"]) == (6, 1, 1, 1)
