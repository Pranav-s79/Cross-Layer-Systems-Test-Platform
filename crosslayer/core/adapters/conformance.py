"""Static conformance checks for adapters.

Any adapter test suite can call ``check_adapter_conformance(MyAdapter())``
and assert the result is empty. The checks need no running system: they
check that what the adapter *declares* matches what it *implements*.
"""

from __future__ import annotations

from crosslayer.core.adapters.base import (
    REQUIRED_CAPABILITIES,
    AdapterDescription,
    Capability,
    SystemAdapter,
)
from crosslayer.core.contracts.model import BoundaryContract

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
