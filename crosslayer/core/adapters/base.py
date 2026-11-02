"""The adapter contract every system under test implements.

An adapter is the only code that knows how a particular system is built,
launched, and logged. The core talks to it through ``SystemAdapter`` and
never imports vendor code.

Lifecycle driven by the orchestrator::

    describe() / capabilities()           what is this, what can it do
    build(ctx)          [BUILD]           compile / prepare (optional)
    execute(ctx)        [EXECUTE]         run the experiment (optional)
    collect(ctx, exec)  [COLLECT]         put raw artifacts in ctx.raw_dir
    normalize(ctx, artifacts) [NORMALIZE] raw artifacts -> NormalizedEvents
    contracts()                           contracts the adapter ships with
    faults()            [FAULT_INJECTION] fault mechanisms it supports

Operations an adapter does not support must stay unimplemented: the base
class raises ``UnsupportedOperation``, and ``capabilities()`` must not
list them. ``check_adapter_conformance`` verifies the two agree.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping, Sequence

from crosslayer.core.contracts.model import BoundaryContract
from crosslayer.core.errors import UnsupportedOperation
from crosslayer.core.telemetry.events import NormalizedEvent


class Capability(StrEnum):
    BUILD = "build"
    EXECUTE = "execute"
    COLLECT = "collect"
    NORMALIZE = "normalize"
    FAULT_INJECTION = "fault_injection"


REQUIRED_CAPABILITIES = frozenset({Capability.COLLECT, Capability.NORMALIZE})
"""Without these an adapter produces no events, so nothing can be verified."""


@dataclass(frozen=True)
class InterfaceSpec:
    """A boundary between two subsystems that contracts can target."""

    name: str
    source: str
    destination: str
    description: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "source": self.source,
            "destination": self.destination,
            "description": self.description,
        }


@dataclass(frozen=True)
class AdapterDescription:
    name: str
    adapter_version: str
    system: str
    system_version: str | None
    """Version or content hash of the system under test, if knowable."""
    subsystems: tuple[str, ...]
    interfaces: tuple[InterfaceSpec, ...] = ()
    description: str = ""
    external_dependencies: tuple[str, ...] = ()
    """Things replay needs that cannot be bundled (simulators, SDKs, images)."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "adapter_version": self.adapter_version,
            "system": self.system,
            "system_version": self.system_version,
            "subsystems": list(self.subsystems),
            "interfaces": [i.to_dict() for i in self.interfaces],
            "description": self.description,
            "external_dependencies": list(self.external_dependencies),
        }


@dataclass(frozen=True)
class EventSpec:
    """One event type an adapter can emit, for linting contracts statically.

    ``attributes`` and ``correlation_ids`` list the keys the event may carry;
    attribute entries may be shell patterns such as ``pipeline.unparsed_*``.
    """

    event_type: str
    subsystems: tuple[str, ...]
    attributes: tuple[str, ...] = ()
    correlation_ids: tuple[str, ...] = ()
    description: str = ""


@dataclass(frozen=True)
class FaultSpec:
    """A fault the adapter can inject, with the ground-truth location it targets.

    ``target_subsystem`` / ``target_interface`` are the labels later used to
    score fault-localization accuracy (top-1 / top-3).
    """

    name: str
    description: str
    target_subsystem: str
    target_interface: str | None = None
    parameters: Mapping[str, str] = field(default_factory=dict)
    """Parameter name -> human description."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "target_subsystem": self.target_subsystem,
            "target_interface": self.target_interface,
            "parameters": dict(self.parameters),
        }


@dataclass(frozen=True)
class FaultRequest:
    name: str
    params: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "params": dict(self.params)}


@dataclass(frozen=True)
class Artifact:
    """A raw file produced by the system under test, stored in the bundle."""

    name: str
    """Path relative to ``raw_artifacts/``, e.g. ``sim.log``."""
    path: Path
    media_type: str = "text/plain"
    description: str = ""


@dataclass(frozen=True)
class RunContext:
    run_id: str
    seed: int
    parameters: Mapping[str, Any]
    faults: tuple[FaultRequest, ...]
    raw_dir: Path
    """Adapters write raw artifacts here (bundle ``raw_artifacts/``)."""
    work_dir: Path
    """Scratch space for build outputs."""
    base_dir: Path
    """Directory relative paths in ``parameters`` resolve against."""
    timeout_s: float
    inputs: tuple[Path, ...] = ()
    """Pre-existing artifacts to analyze (log-only mode)."""

    def raw_ref(self, artifact_name: str, line: int | None = None) -> str:
        ref = f"raw_artifacts/{artifact_name}"
        return f"{ref}#L{line}" if line is not None else ref

    def resolve(self, value: str | Path) -> Path:
        path = Path(value)
        return path if path.is_absolute() else (self.base_dir / path).resolve()


@dataclass
class BuildResult:
    ok: bool
    details: dict[str, Any] = field(default_factory=dict)


class ExecutionStatus(StrEnum):
    COMPLETED = "completed"
    """The process ran to completion (with any exit code)."""
    TIMEOUT = "timeout"
    FAILED_TO_START = "failed_to_start"


@dataclass
class ExecutionResult:
    status: ExecutionStatus
    exit_code: int | None
    duration_s: float
    command: Sequence[str] = ()
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": str(self.status),
            "exit_code": self.exit_code,
            "duration_s": round(self.duration_s, 6),
            "command": list(self.command),
            "details": self.details,
        }


@dataclass(frozen=True)
class ParseDiagnostic:
    """Raw input the adapter could not normalize. Reported, never silently dropped."""

    artifact: str
    line: int | None
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {"artifact": self.artifact, "line": self.line, "message": self.message}


@dataclass
class NormalizationResult:
    events: list[NormalizedEvent]
    diagnostics: list[ParseDiagnostic] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)


class SystemAdapter(ABC):
    @abstractmethod
    def describe(self) -> AdapterDescription: ...

    @abstractmethod
    def capabilities(self) -> frozenset[Capability]: ...

    def validate_parameters(self, parameters: Mapping[str, Any]) -> None:
        """Raise ``ConfigError`` for bad parameters before anything runs."""

    def validate_faults(self, faults: Sequence[FaultRequest]) -> None:
        """Raise ``ConfigError`` for bad fault parameter *values* before anything
        runs. Unknown fault names and parameter names are already rejected by
        the core using ``faults()``."""

    def build(self, ctx: RunContext) -> BuildResult:
        raise UnsupportedOperation(f"{type(self).__name__} does not support build")

    def execute(self, ctx: RunContext) -> ExecutionResult:
        raise UnsupportedOperation(f"{type(self).__name__} does not support execute")

    def collect(self, ctx: RunContext, execution: ExecutionResult | None) -> list[Artifact]:
        raise UnsupportedOperation(f"{type(self).__name__} does not support collect")

    def normalize(self, ctx: RunContext, artifacts: Sequence[Artifact]) -> NormalizationResult:
        raise UnsupportedOperation(f"{type(self).__name__} does not support normalize")

    def contracts(self) -> list[BoundaryContract]:
        return []

    def faults(self) -> list[FaultSpec]:
        return []

    def event_catalog(self) -> list[EventSpec] | None:
        """Event types this adapter can emit, or None if unknown.

        Optional. When provided, ``crosslayer lint`` can detect contracts that
        can never match (misspelled event types, attributes, or correlation
        ids) before anything runs.
        """
        return None
