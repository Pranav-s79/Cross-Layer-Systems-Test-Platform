"""Contract tests: verify the verifier.

A contract that has never been seen to fail is untested. A contract test
file pairs each contract with small traces it must accept (known-good) and
traces it must reject (known-bad), and asserts the verdict::

    schema_version: 1
    adapter: sensor_pipeline        # load this adapter's default contracts, and/or
    contracts: [contracts.yaml]     # contract files relative to this file
    cases:
      - name: processed packet is accounted for
        contract: pipeline.rx_accounted
        expect: pass
        events:                     # inline trace (compact form, see below)
          - {t_us: 0, type: system.boot, subsystem: sys}
          - {t_us: 10, type: packet.received, subsystem: sensor_rx, ids: {packet: 1}}
          - {t_us: 90, type: packet.processed, subsystem: proc, ids: {packet: 1}}
          - {t_us: 99, type: system.halt, subsystem: sys}
      - name: recorded silent drop is caught
        contract: pipeline.rx_accounted
        expect: fail
        expect_violations: 3
        trace: fixtures/silent_drop.jsonl   # NormalizedEvent JSONL, e.g. from a run bundle

Compact inline events use ``t_ns`` / ``t_us`` / ``t_ms`` (exactly one),
``type``, and optional ``subsystem``, ``source``, ``severity``, ``ids``,
``attrs``. Traces are evaluated without any adapter or system under test, so
contract tests are fast and deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from crosslayer.core.adapters.registry import load_adapter
from crosslayer.core.contracts.evaluator import evaluate_contract
from crosslayer.core.contracts.loader import ensure_unique_ids, load_contracts_file, read_document
from crosslayer.core.contracts.model import BoundaryContract
from crosslayer.core.errors import ConfigError
from crosslayer.core.telemetry.events import NormalizedEvent
from crosslayer.core.telemetry.store import order_events, read_events_jsonl
from crosslayer.core.units import ms_to_ns, us_to_ns
from crosslayer.core.verdict import Verdict

SCHEMA_VERSION = 1
_DOC_KEYS = {"schema_version", "adapter", "contracts", "cases"}
_CASE_KEYS = {"name", "contract", "expect", "events", "trace", "expect_violations",
              "expect_unresolved", "description"}
_EVENT_KEYS = {"t_ns", "t_us", "t_ms", "type", "subsystem", "source", "severity", "ids", "attrs"}


@dataclass
class ContractTestCase:
    name: str
    contract_id: str
    expect: Verdict
    events: list[NormalizedEvent]
    expect_violations: int | None = None
    expect_unresolved: int | None = None
    description: str = ""


@dataclass
class CaseResult:
    case: ContractTestCase
    actual: Verdict
    passed: bool
    problems: list[str] = field(default_factory=list)
    summary: str = ""


@dataclass
class ContractTestReport:
    source: str
    results: list[CaseResult]
    contracts: list[BoundaryContract]

    @property
    def passed(self) -> bool:
        return all(r.passed for r in self.results)

    def coverage(self) -> dict[str, set[Verdict]]:
        """Contract id -> verdicts that have been demonstrated by passing cases."""
        seen: dict[str, set[Verdict]] = {c.id: set() for c in self.contracts}
        for r in self.results:
            if r.passed:
                seen.setdefault(r.case.contract_id, set()).add(r.case.expect)
        return seen

    def uncovered(self) -> list[str]:
        """Contracts lacking a passing known-good case or a passing known-bad case."""
        return sorted(
            cid for cid, verdicts in self.coverage().items()
            if Verdict.PASS not in verdicts or Verdict.FAIL not in verdicts
        )


def _inline_event(raw: Any, index: int, location: str) -> NormalizedEvent:
    where = f"{location}.events[{index}]"
    if not isinstance(raw, Mapping):
        raise ConfigError("event must be a mapping", location=where)
    unknown = raw.keys() - _EVENT_KEYS
    if unknown:
        raise ConfigError(f"unknown keys {sorted(unknown)} (allowed: {sorted(_EVENT_KEYS)})", location=where)
    times = [k for k in ("t_ns", "t_us", "t_ms") if k in raw]
    if len(times) != 1:
        raise ConfigError("give exactly one of t_ns, t_us, t_ms", location=where)
    key = times[0]
    value = raw[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError("must be a number", location=f"{where}.{key}")
    try:
        if key == "t_ns":
            if not isinstance(value, int):
                raise ValueError("t_ns must be an integer")
            t = value
        elif key == "t_us":
            t = us_to_ns(value)
        else:
            t = ms_to_ns(value)
        return NormalizedEvent(
            run_id="contract-test",
            event_id=f"inline:{index}",
            source_system=raw.get("source", "fixture"),
            subsystem=raw.get("subsystem", "fixture"),
            timestamp_ns=t,
            event_type=raw.get("type", ""),
            severity=raw.get("severity", "info"),
            correlation_ids=raw.get("ids") or {},
            attributes=raw.get("attrs") or {},
            raw_artifact_ref=f"{location}#event{index}",
        )
    except (ValueError, TypeError) as exc:
        raise ConfigError(str(exc), location=where) from exc


def _load_contracts(doc: Mapping[str, Any], base: Path, source: str) -> list[BoundaryContract]:
    contracts: list[BoundaryContract] = []
    if "adapter" in doc:
        if not isinstance(doc["adapter"], str):
            raise ConfigError("must be an adapter name", location=f"{source}: adapter")
        contracts += load_adapter(doc["adapter"]).contracts()
    files = doc.get("contracts") or []
    if not isinstance(files, list) or not all(isinstance(f, str) for f in files):
        raise ConfigError("must be a list of file paths", location=f"{source}: contracts")
    for f in files:
        contracts += load_contracts_file((base / f).resolve())
    if not contracts:
        raise ConfigError("no contracts: give 'adapter' and/or 'contracts'", location=source)
    ensure_unique_ids(contracts)
    return contracts


def load_contract_tests(path: Path) -> tuple[list[BoundaryContract], list[ContractTestCase]]:
    path = path.resolve()
    source = str(path)
    doc = read_document(path)
    if not isinstance(doc, Mapping):
        raise ConfigError("contract test file must be a mapping", location=source)
    unknown = doc.keys() - _DOC_KEYS
    if unknown:
        raise ConfigError(f"unknown keys {sorted(unknown)}", location=source)
    if doc.get("schema_version") != SCHEMA_VERSION:
        raise ConfigError(f"schema_version must be {SCHEMA_VERSION}", location=source)

    contracts = _load_contracts(doc, path.parent, source)
    known = {c.id for c in contracts}

    raw_cases = doc.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ConfigError("'cases' must be a non-empty list", location=source)
    cases: list[ContractTestCase] = []
    names: set[str] = set()
    for i, raw in enumerate(raw_cases):
        where = f"{source}: cases[{i}]"
        if not isinstance(raw, Mapping):
            raise ConfigError("case must be a mapping", location=where)
        unknown = raw.keys() - _CASE_KEYS
        if unknown:
            raise ConfigError(f"unknown keys {sorted(unknown)}", location=where)
        for key in ("name", "contract", "expect"):
            if not isinstance(raw.get(key), str) or not raw[key]:
                raise ConfigError(f"'{key}' is required", location=where)
        if raw["name"] in names:
            raise ConfigError(f"duplicate case name {raw['name']!r}", location=where)
        names.add(raw["name"])
        if raw["contract"] not in known:
            raise ConfigError(f"unknown contract {raw['contract']!r}", location=where)
        try:
            expect = Verdict(raw["expect"])
        except ValueError:
            raise ConfigError(f"expect must be one of {[v.value for v in Verdict]}", location=where) from None

        if ("events" in raw) == ("trace" in raw):
            raise ConfigError("give exactly one of 'events' or 'trace'", location=where)
        if "events" in raw:
            if not isinstance(raw["events"], list):
                raise ConfigError("'events' must be a list", location=where)
            events = [_inline_event(e, n, where) for n, e in enumerate(raw["events"])]
        else:
            trace = (path.parent / str(raw["trace"])).resolve()
            try:
                events = read_events_jsonl(trace)
            except OSError as exc:
                raise ConfigError(f"cannot read trace: {exc}", location=where) from exc
            except ValueError as exc:
                raise ConfigError(f"invalid trace: {exc}", location=where) from exc

        counts = {}
        for key in ("expect_violations", "expect_unresolved"):
            value = raw.get(key)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                raise ConfigError("must be a non-negative integer", location=f"{where}.{key}")
            counts[key] = value

        cases.append(
            ContractTestCase(
                name=raw["name"],
                contract_id=raw["contract"],
                expect=expect,
                events=order_events(events),
                expect_violations=counts["expect_violations"],
                expect_unresolved=counts["expect_unresolved"],
                description=str(raw.get("description") or ""),
            )
        )
    return contracts, cases


def run_case(contract: BoundaryContract, case: ContractTestCase) -> CaseResult:
    result = evaluate_contract(contract, case.events)
    problems = []
    if result.verdict is not case.expect:
        problems.append(f"expected {case.expect.upper()}, got {result.verdict.upper()}: {result.summary}")
    for key, expected in (("violations", case.expect_violations), ("unresolved", case.expect_unresolved)):
        if expected is not None:
            actual = result.stats.get(key)
            if actual != expected:
                problems.append(f"expected {expected} {key}, got {actual}")
    return CaseResult(case=case, actual=result.verdict, passed=not problems, problems=problems,
                      summary=result.summary)


def run_contract_tests(path: Path) -> ContractTestReport:
    contracts, cases = load_contract_tests(path)
    by_id = {c.id: c for c in contracts}
    results = [run_case(by_id[case.contract_id], case) for case in cases]
    return ContractTestReport(source=str(path), results=results, contracts=contracts)
