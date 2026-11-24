"""Failure branches of core/adapters/conformance.py, fed deliberately broken adapters."""

from __future__ import annotations

import pytest

from crosslayer.core.adapters import (
    AdapterDescription,
    Capability,
    EventSpec,
    FaultSpec,
    InterfaceSpec,
    NormalizationResult,
    SystemAdapter,
    check_adapter_conformance,
    check_events_against_catalog,
)
from crosslayer.core.contracts.model import BoundaryContract, Rule, RuleOutcome
from crosslayer.core.telemetry.events import NormalizedEvent
from crosslayer.core.verdict import Verdict


class _NoopRule(Rule):
    kind = "noop"

    @classmethod
    def parse(cls, raw, location):
        return cls()

    def evaluate(self, events):
        return RuleOutcome(Verdict.PASS, "ok")


def _contract(cid: str, source: str = "a", destination: str = "b") -> BoundaryContract:
    return BoundaryContract(cid, "t", "o", source, destination, _NoopRule())


class Base(SystemAdapter):
    """Conforming baseline; each test overrides one thing."""

    def describe(self):
        return AdapterDescription("base", "1.0", "sys", None, ("a", "b"), (InterfaceSpec("a_to_b", "a", "b"),))

    def capabilities(self):
        return frozenset({Capability.COLLECT, Capability.NORMALIZE})

    def collect(self, ctx, execution):
        return []

    def normalize(self, ctx, artifacts):
        return NormalizationResult(events=[])


def _problems(**overrides):
    return check_adapter_conformance(type("Broken", (Base,), overrides)())


def test_baseline_is_clean():
    assert check_adapter_conformance(Base()) == []


def test_describe_wrong_type_returns_single_problem():
    assert _problems(describe=lambda self: {"name": "x"}) == ["describe() must return an AdapterDescription"]


@pytest.mark.parametrize(
    "name, version",
    [("", "1.0"), ("x", "")],
)
def test_describe_requires_name_and_version(name, version):
    desc = AdapterDescription(name, version, "sys", None, ("a", "b"))
    assert "describe() must set name and adapter_version" in _problems(describe=lambda self: desc)


def test_describe_requires_a_subsystem():
    desc = AdapterDescription("x", "1.0", "sys", None, ())
    assert "describe() must declare at least one subsystem" in _problems(describe=lambda self: desc)


def test_capabilities_must_be_frozenset_of_capability():
    for bad in ({Capability.COLLECT}, frozenset({"collect"})):
        problems = _problems(capabilities=lambda self, bad=bad: bad)
        assert "capabilities() must return a frozenset of Capability" in problems
        # Falls back to no capabilities, so both required ones are reported missing.
        assert "capability 'collect' is required" in problems
        assert "capability 'normalize' is required" in problems


def test_faults_declared_without_capability():
    faults = lambda self: [FaultSpec("f", "d", "a")]  # noqa: E731
    assert "faults() is non-empty but fault_injection is not declared" in _problems(faults=faults)


def test_duplicate_fault_names():
    class Dup(Base):
        def capabilities(self):
            return super().capabilities() | {Capability.FAULT_INJECTION}

        def faults(self):
            return [FaultSpec("f", "d", "a"), FaultSpec("f", "d2", "b")]

    assert "duplicate fault name 'f'" in check_adapter_conformance(Dup())


def test_interface_references_undeclared_subsystem():
    desc = AdapterDescription("x", "1.0", "sys", None, ("a",), (InterfaceSpec("a_to_z", "a", "z"),))
    problems = _problems(describe=lambda self: desc)
    assert problems == ["interface 'a_to_z' references undeclared subsystem 'z'"]


def test_event_catalog_duplicates_and_undeclared_subsystem():
    catalog = [EventSpec("x.one", ("a",)), EventSpec("x.one", ("a",)), EventSpec("x.two", ("nowhere",))]
    problems = _problems(event_catalog=lambda self: catalog)
    assert "event catalog lists 'x.one' twice" in problems
    assert "event catalog: 'x.two' uses undeclared subsystem 'nowhere'" in problems


def test_contracts_must_be_boundary_contracts():
    assert "contracts() must return BoundaryContract objects" in _problems(contracts=lambda self: ["nope"])


def test_duplicate_contract_ids_and_undeclared_endpoints():
    contracts = [_contract("c1"), _contract("c1"), _contract("c2", source="x", destination="y")]
    problems = _problems(contracts=lambda self: contracts)
    assert "duplicate contract id 'c1'" in problems
    assert "contract 'c2' references undeclared subsystem 'x'" in problems
    assert "contract 'c2' references undeclared subsystem 'y'" in problems


# -- check_events_against_catalog ------------------------------------------------


def _event(event_type="x.one", subsystem="a", attributes=None, correlation_ids=None):
    return NormalizedEvent(
        run_id="r",
        event_id="e1",
        source_system="sys",
        subsystem=subsystem,
        timestamp_ns=1,
        event_type=event_type,
        attributes=attributes or {},
        correlation_ids=correlation_ids or {},
    )


CATALOG = [EventSpec("x.one", ("a",), ("x.size", "x.tag_*"), ("pkt",))]


def test_events_within_catalog_are_clean():
    events = [_event(attributes={"x.size": 1, "x.tag_x": 2}, correlation_ids={"pkt": "1"})]
    assert check_events_against_catalog(events, CATALOG) == []


@pytest.mark.parametrize(
    "event, expected",
    [
        (_event(event_type="x.other"), "event type 'x.other' is not in the catalog"),
        (_event(subsystem="b"), "'x.one' emitted by undeclared subsystem 'b'"),
        (_event(attributes={"x.bogus": 1}), "'x.one' carries undeclared attribute 'x.bogus'"),
        (_event(correlation_ids={"zzz": "1"}), "'x.one' carries undeclared correlation id 'zzz'"),
    ],
)
def test_events_outside_catalog_are_reported(event, expected):
    assert check_events_against_catalog([event], CATALOG) == [expected]


def test_catalog_problems_are_deduplicated_and_sorted():
    events = [_event(attributes={"x.zz": 1}), _event(attributes={"x.zz": 1}), _event(attributes={"x.aa": 1})]
    assert check_events_against_catalog(events, CATALOG) == [
        "'x.one' carries undeclared attribute 'x.aa'",
        "'x.one' carries undeclared attribute 'x.zz'",
    ]
