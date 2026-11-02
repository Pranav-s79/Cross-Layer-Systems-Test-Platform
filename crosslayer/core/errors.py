"""Exception hierarchy shared by every core layer."""

from __future__ import annotations


class CrossLayerError(Exception):
    """Base class for all CrossLayer errors."""


class ConfigError(CrossLayerError):
    """An experiment, contract, or adapter configuration is invalid.

    ``location`` points at the offending field (for example
    ``contracts.yaml: contracts[2].rule.within_ms``) so the author can fix it
    without reading a stack trace.
    """

    def __init__(self, message: str, *, location: str | None = None) -> None:
        self.location = location
        super().__init__(f"{location}: {message}" if location else message)


class ExpressionError(ConfigError):
    """A contract expression failed static validation."""


class UnsupportedOperation(CrossLayerError):
    """An adapter was asked to perform an operation it declared unsupported."""


class AdapterError(CrossLayerError):
    """An adapter failed while performing an operation it claims to support."""


class MissingEvidence(CrossLayerError):
    """Evaluation needed data (an attribute, a correlation id) that was not observed.

    Rules turn this into an *unresolved* finding instead of a failure: absence
    of data is not proof of a defect.
    """
