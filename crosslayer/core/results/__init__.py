"""Results store: a SQLite index over run bundles (roadmap Phase 11)."""

from crosslayer.core.results.index import (
    DEFAULT_DB,
    SCHEMA_VERSION,
    AddOutcome,
    RunIndex,
    parse_since,
)

from crosslayer.core.results.flaky import FlakyFinding, find_flaky, repeated_series
from crosslayer.core.results.flaky import render as render_flaky
from crosslayer.core.results.regressions import Tolerances, analyze, has_regression, render

__all__ = [
    "DEFAULT_DB", "SCHEMA_VERSION", "AddOutcome", "FlakyFinding", "RunIndex", "Tolerances", "analyze", "find_flaky",
    "has_regression", "parse_since", "render", "render_flaky", "repeated_series",
]
