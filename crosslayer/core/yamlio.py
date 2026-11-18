"""YAML parsing for every CrossLayer document, with the same safe semantics everywhere.

PyYAML's pure-Python parser dominates the cost of loading small documents
(profiles, contracts, experiments), so this uses the libyaml-backed
``CSafeLoader`` when PyYAML was built with it, and ``SafeLoader`` otherwise.
Both construct only plain data (no arbitrary Python objects).
"""

from __future__ import annotations

from typing import Any

import yaml

YAMLError = yaml.YAMLError
SAFE_LOADER: type = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
"""The loader in use; ``CSafeLoader`` if libyaml is available."""


def load_yaml(text: str) -> Any:
    """Parse one YAML document into plain data (dicts, lists, scalars)."""
    return yaml.load(text, Loader=SAFE_LOADER)  # noqa: S506 - a safe loader, never yaml.Loader
