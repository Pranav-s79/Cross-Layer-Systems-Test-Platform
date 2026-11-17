"""Direct tests for adapter discovery in core/adapters/registry.py."""

from __future__ import annotations

from importlib.metadata import EntryPoint

import pytest

from crosslayer.core.adapters import registry
from crosslayer.core.adapters.registry import available_adapters, load_adapter
from crosslayer.core.errors import ConfigError

SENSOR = "crosslayer.integrations.sensor_pipeline:SensorPipelineAdapter"


def _patch_entry_points(monkeypatch, points):
    monkeypatch.setattr(
        registry,
        "entry_points",
        lambda group: [EntryPoint(name=n, value=v, group=group) for n, v in points],
    )


def test_unknown_name_lists_registered_adapters():
    with pytest.raises(ConfigError, match=r"unknown adapter 'nope'.*sensor_pipeline"):
        load_adapter("nope")


def test_unknown_name_with_no_adapters_installed(monkeypatch):
    _patch_entry_points(monkeypatch, [])
    with pytest.raises(ConfigError, match=r"\(none installed\)"):
        load_adapter("anything")


def test_duplicate_registration_last_entry_wins(monkeypatch):
    _patch_entry_points(monkeypatch, [("dup", "pkg.a:A"), ("dup", SENSOR)])
    assert available_adapters() == {"dup": SENSOR}
    assert type(load_adapter("dup")).__name__ == "SensorPipelineAdapter"


@pytest.mark.parametrize(
    ("reference", "message"),
    [
        ("pkg:", "must look like"),
        (":Class", "must look like"),
        ("no_such_module_xyz:Cls", "cannot import adapter module"),
        ("crosslayer.core.adapters.registry:Missing", "has no attribute"),
        ("crosslayer.core.adapters.registry:ENTRY_POINT_GROUP", "not a SystemAdapter subclass"),
    ],
)
def test_bad_module_class_reference(reference, message):
    with pytest.raises(ConfigError, match=message):
        load_adapter(reference)


def test_module_class_reference_loads_without_registration(monkeypatch):
    _patch_entry_points(monkeypatch, [])
    assert type(load_adapter(SENSOR)).__name__ == "SensorPipelineAdapter"
