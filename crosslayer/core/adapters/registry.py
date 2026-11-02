"""Adapter discovery.

Adapters register through the ``crosslayer.adapters`` entry-point group in
their package metadata::

    [project.entry-points."crosslayer.adapters"]
    sensor_pipeline = "crosslayer.integrations.sensor_pipeline:SensorPipelineAdapter"

An experiment can also name an adapter directly as ``"package.module:Class"``,
which is useful during development before an entry point exists.

Because discovery goes through package metadata, the core never imports an
integration module by name.
"""

from __future__ import annotations

import importlib
from importlib.metadata import entry_points

from crosslayer.core.adapters.base import SystemAdapter
from crosslayer.core.errors import ConfigError

ENTRY_POINT_GROUP = "crosslayer.adapters"


def available_adapters() -> dict[str, str]:
    """Registered adapter name -> ``module:attr`` target."""
    return {ep.name: ep.value for ep in entry_points(group=ENTRY_POINT_GROUP)}


def _load_target(target: str) -> object:
    module_name, _, attr = target.partition(":")
    if not module_name or not attr:
        raise ConfigError(f"adapter reference {target!r} must look like 'package.module:Class'")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise ConfigError(f"cannot import adapter module {module_name!r}: {exc}") from exc
    try:
        return getattr(module, attr)
    except AttributeError:
        raise ConfigError(f"module {module_name!r} has no attribute {attr!r}") from None


def load_adapter(reference: str) -> SystemAdapter:
    """Instantiate an adapter by registered name or ``module:Class`` path."""
    if ":" in reference:
        target = reference
    else:
        registered = available_adapters()
        if reference not in registered:
            known = ", ".join(sorted(registered)) or "(none installed)"
            raise ConfigError(f"unknown adapter {reference!r}; registered adapters: {known}")
        target = registered[reference]

    cls = _load_target(target)
    if not isinstance(cls, type) or not issubclass(cls, SystemAdapter):
        raise ConfigError(f"{target!r} is not a SystemAdapter subclass")
    return cls()
