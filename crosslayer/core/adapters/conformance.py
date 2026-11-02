"""Static conformance checks for adapters.

Any adapter test suite can call ``check_adapter_conformance(MyAdapter())``
and assert the result is empty. The checks need no running system: they
check that what the adapter *declares* matches what it *implements*.
"""

from __future__ import annotations

from fnmatch import fnmatchcase
from typing import Iterable

from crosslayer.core.adapters.base import (
    REQUIRED_CAPABILITIES,
    AdapterDescription,
    Capability,
    EventSpec,
    SystemAdapter,
)
from crosslayer.core.contracts.model import BoundaryContract
from crosslayer.core.telemetry.events import NormalizedEvent

_OPERATION_FOR = {
    Capability.BUILD: "build",
    Capability.EXECUTE: "execute",
    Capability.COLLECT: "collect",
    Capability.NORMALIZE: "normalize",
}


def _overrides(adapter: SystemAdapter, method: str) -> bool:
    return getattr(type(adapter), method) is not getattr(SystemAdapter, method)


def check_adapter_conformance(adapter: SystemAdapter) -> list[str]:
    problems: list[str] = []

    description = adapter.describe()
    if not isinstance(description, AdapterDescription):
        return ["describe() must return an AdapterDescription"]
    if not description.name or not description.adapter_version:
        problems.append("describe() must set name and adapter_version")
    if not description.subsystems:
        problems.append("describe() must declare at least one subsystem")
    subsystems = set(description.subsystems)

    caps = adapter.capabilities()
    if not isinstance(caps, frozenset) or not all(isinstance(c, Capability) for c in caps):
        problems.append("capabilities() must return a frozenset of Capability")
        caps = frozenset()

    for cap in REQUIRED_CAPABILITIES - caps:
        problems.append(f"capability '{cap}' is required")

    for cap, method in _OPERATION_FOR.items():
        declared = cap in caps
        implemented = _overrides(adapter, method)
        if declared and not implemented:
            problems.append(f"declares '{cap}' but does not implement {method}()")
        if implemented and not declared:
            problems.append(f"implements {method}() but does not declare '{cap}'; declare it or remove it")

    faults = adapter.faults()
    if Capability.FAULT_INJECTION in caps and not faults:
        problems.append("declares fault_injection but faults() is empty")
    if faults and Capability.FAULT_INJECTION not in caps:
        problems.append("faults() is non-empty but fault_injection is not declared")
    interface_names = {i.name for i in description.interfaces}
    seen_faults: set[str] = set()
    for fault in faults:
        if fault.name in seen_faults:
            problems.append(f"duplicate fault name '{fault.name}'")
        seen_faults.add(fault.name)
        if fault.target_subsystem not in subsystems:
            problems.append(f"fault '{fault.name}' targets undeclared subsystem '{fault.target_subsystem}'")
        if fault.target_interface is not None and fault.target_interface not in interface_names:
            problems.append(f"fault '{fault.name}' targets undeclared interface '{fault.target_interface}'")

    for interface in description.interfaces:
        for end in (interface.source, interface.destination):
            if end not in subsystems:
                problems.append(f"interface '{interface.name}' references undeclared subsystem '{end}'")

    catalog = adapter.event_catalog()
    if catalog is not None:
        seen_types: set[str] = set()
        for spec in catalog:
            if spec.event_type in seen_types:
                problems.append(f"event catalog lists '{spec.event_type}' twice")
            seen_types.add(spec.event_type)
            for sub in spec.subsystems:
                if sub not in subsystems:
                    problems.append(f"event catalog: '{spec.event_type}' uses undeclared subsystem '{sub}'")

    contracts = adapter.contracts()
    seen_ids: set[str] = set()
    for contract in contracts:
        if not isinstance(contract, BoundaryContract):
            problems.append("contracts() must return BoundaryContract objects")
            continue
        if contract.id in seen_ids:
            problems.append(f"duplicate contract id '{contract.id}'")
        seen_ids.add(contract.id)
        for end in (contract.source, contract.destination):
            if end not in subsystems:
                problems.append(f"contract '{contract.id}' references undeclared subsystem '{end}'")

    return problems


def check_events_against_catalog(events: Iterable[NormalizedEvent], catalog: list[EventSpec]) -> list[str]:
    """Check that emitted events stay within the adapter's declared catalog.

    Run this on real output in an adapter's tests: a catalog that lies makes
    the linter give wrong answers.
    """
    specs = {s.event_type: s for s in catalog}
    problems: set[str] = set()
    for e in events:
        spec = specs.get(e.event_type)
        if spec is None:
            problems.add(f"event type '{e.event_type}' is not in the catalog")
            continue
        if e.subsystem not in spec.subsystems:
            problems.add(f"'{e.event_type}' emitted by undeclared subsystem '{e.subsystem}'")
        for key in e.attributes:
            if not any(fnmatchcase(key, pattern) for pattern in spec.attributes):
                problems.add(f"'{e.event_type}' carries undeclared attribute '{key}'")
        for key in e.correlation_ids:
            if key not in spec.correlation_ids:
                problems.add(f"'{e.event_type}' carries undeclared correlation id '{key}'")
    return sorted(problems)
