"""Adapter conformance and discovery."""

from __future__ import annotations

from crosslayer.core.adapters import (
    AdapterDescription,
    Capability,
    FaultSpec,
    InterfaceSpec,
    NormalizationResult,
    SystemAdapter,
    available_adapters,
    check_adapter_conformance,
    load_adapter,
)
from crosslayer.core.errors import ConfigError, UnsupportedOperation
import pytest


class LogOnlyAdapter(SystemAdapter):
    """Collects and normalizes, but cannot build or execute."""

    def describe(self):
        return AdapterDescription("log_only", "1.0", "logs", None, ("a", "b"),
                                  (InterfaceSpec("a_to_b", "a", "b"),))

    def capabilities(self):
        return frozenset({Capability.COLLECT, Capability.NORMALIZE})

    def collect(self, ctx, execution):
        return []

    def normalize(self, ctx, artifacts):
        return NormalizationResult(events=[])


def test_conforming_adapter_has_no_problems():
    assert check_adapter_conformance(LogOnlyAdapter()) == []


def test_unsupported_operations_raise():
    with pytest.raises(UnsupportedOperation):
        LogOnlyAdapter().execute(None)  # type: ignore[arg-type]


def test_declared_but_not_implemented():
    class Liar(LogOnlyAdapter):
        def capabilities(self):
            return frozenset({Capability.COLLECT, Capability.NORMALIZE, Capability.EXECUTE})

    assert any("does not implement execute" in p for p in check_adapter_conformance(Liar()))


def test_implemented_but_not_declared():
    class Hidden(LogOnlyAdapter):
        def build(self, ctx):
            raise NotImplementedError

    assert any("implements build" in p for p in check_adapter_conformance(Hidden()))


def test_missing_required_capability():
    class Blind(LogOnlyAdapter):
        def capabilities(self):
            return frozenset({Capability.COLLECT})

        normalize = SystemAdapter.normalize

    problems = check_adapter_conformance(Blind())
    assert any("'normalize' is required" in p for p in problems)


def test_fault_targets_must_be_declared():
    class BadFaults(LogOnlyAdapter):
        def capabilities(self):
            return super().capabilities() | {Capability.FAULT_INJECTION}

        def faults(self):
            return [FaultSpec("f", "d", target_subsystem="nowhere", target_interface="nope")]

    problems = check_adapter_conformance(BadFaults())
    assert any("undeclared subsystem 'nowhere'" in p for p in problems)
    assert any("undeclared interface 'nope'" in p for p in problems)


def test_fault_injection_requires_faults():
    class Empty(LogOnlyAdapter):
        def capabilities(self):
            return super().capabilities() | {Capability.FAULT_INJECTION}

    assert any("faults() is empty" in p for p in check_adapter_conformance(Empty()))


def test_entry_point_discovery_finds_reference_adapter():
    assert "sensor_pipeline" in available_adapters()
    adapter = load_adapter("sensor_pipeline")
    assert adapter.describe().name == "sensor_pipeline"


def test_load_by_module_path():
    adapter = load_adapter("tests.core.test_adapters:LogOnlyAdapter")
    assert isinstance(adapter, LogOnlyAdapter)


@pytest.mark.parametrize(
    "reference",
    ["does_not_exist", "no_colon_module.thing:", "tests.core.test_adapters:Missing", "tests.core.test_adapters:pytest"],
)
def test_load_errors(reference):
    with pytest.raises(ConfigError):
        load_adapter(reference)
