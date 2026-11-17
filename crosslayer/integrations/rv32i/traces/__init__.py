"""Trace sources: turn whatever a core's testbench produced into typed records.

Every source yields the same :class:`Record` kinds with the same field names
(the RVFI vocabulary below), so normalization, the reference model, and the
contracts never depend on how a core reports what it retired. A source that
cannot observe a field leaves it out; the contracts that need it then report
SKIPPED or INCONCLUSIVE instead of passing on missing data.

Testbench markers (``BOOT``, ``OUT``, ``BUSERR``, ``HALT``, ``TIMEOUT``) are
shared by every source and parsed by :func:`parse_marker`.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Union

from crosslayer.core.adapters import ParseDiagnostic

TRACE_LOG = "trace.log"
"""Simulator stdout: markers, plus retirements for text sources."""

KINDS = ("boot", "retire", "output", "bus_error", "halt", "timeout")
RETIRE_FIELDS = (
    "cyc", "ord", "pc", "insn", "rs1", "rs1v", "rs2", "rs2v", "rd", "rdv", "npc", "maddr", "wmask", "wdata", "trap",
)
"""Field names of a retirement, after the RISC-V Formal Interface (RVFI):
cycle, order, PC, instruction, source registers and values, destination
register and value, next PC, data address, byte write mask, write data, trap."""


@dataclass(frozen=True)
class Record:
    kind: str
    artifact: str
    line: int
    fields: Mapping[str, int] = field(default_factory=dict)


Item = Union[Record, ParseDiagnostic]
Fetch = Callable[[int], int]
"""Returns the instruction word at a PC from the loaded program image."""


class TraceSource(ABC):
    """How one trace format is produced (simulator plusargs) and parsed."""

    name: str
    timed: bool
    """True if retirements carry the cycle (``cyc``) they retired on."""
    operands: bool
    """True if retirements carry the register values read (``rs1v``, ``rs2v``)."""
    artifacts: tuple[str, ...] = (TRACE_LOG,)
    """Raw files (in the run's raw directory) the source reads."""

    def for_profile(self, signals: Mapping[str, Any] | None) -> "TraceSource":
        """The source configured for one core profile (its ``signals`` map, if any)."""
        return self

    def plusargs(self) -> list[str]:
        """Extra ``vvp`` plusargs that make the testbench emit this format."""
        return []

    @abstractmethod
    def parse(self, raw_dir: Path, fetch: Fetch) -> Iterator[Item]:
        """Yield records in trace order, and diagnostics for lines that can't be used."""


_MARKER_RE = re.compile(r"^(?P<tag>[A-Z]+)(?P<rest>(?:\s+[a-z0-9_]+=[0-9a-fA-FxXzZ]+)*)\s*$")
_MARKERS = {"BOOT": "boot", "OUT": "output", "BUSERR": "bus_error", "HALT": "halt", "TIMEOUT": "timeout"}
_DECIMAL = {"cyc", "ord", "rs1", "rs2", "rd", "trap", "retired", "max_cycles", "wait_states"}
_IGNORED = ("VCD info:",)
"""Simulator chatter, not trace content."""
REQUIRED: dict[str, tuple[str, ...]] = {
    "retire": RETIRE_FIELDS,
    "output": ("cyc", "data"),
    "bus_error": ("cyc", "addr"),
    "halt": ("cyc", "retired"),
    "timeout": ("cyc", "retired"),
}


def ignorable(line: str) -> bool:
    return not line.strip() or line.startswith(_IGNORED)


def parse_tagged(line: str) -> tuple[str, dict[str, int]] | str:
    """Parse ``TAG key=value ...`` into the tag and integer fields, or return an error message.

    Values are hex except the keys in ``_DECIMAL``.
    """
    match = _MARKER_RE.match(line)
    if match is None:
        return f"unrecognized line: {line[:120]!r}"
    try:
        fields = {k: (int(v) if k in _DECIMAL else int(v, 16))
                  for k, v in (kv.split("=", 1) for kv in match["rest"].split())}
    except ValueError:
        return f"non-numeric value (X or Z?) in: {line[:120]!r}"
    return match["tag"], fields


def parse_marker(artifact: str, lineno: int, line: str) -> Item | None:
    """A testbench marker line as a record, a diagnostic, or None if it is not a marker."""
    parsed = parse_tagged(line)
    if isinstance(parsed, str) or parsed[0] not in _MARKERS:
        return None
    tag, fields = parsed
    kind = _MARKERS[tag]
    return checked(Record(kind, artifact, lineno, fields), tag)


def checked(record: Record, label: str) -> Item:
    """The record, or a diagnostic if it lacks a field its kind requires."""
    missing = [k for k in REQUIRED.get(record.kind, ()) if k not in record.fields]
    if missing:
        return ParseDiagnostic(record.artifact, record.line, f"{label} line missing {missing}")
    return record


def sources() -> dict[str, TraceSource]:
    from crosslayer.integrations.rv32i.traces.commitlog import CommitLogSource
    from crosslayer.integrations.rv32i.traces.rvfi import RvfiSource
    from crosslayer.integrations.rv32i.traces.vcd import VcdSignalsSource

    return {s.name: s for s in (RvfiSource(), CommitLogSource(), VcdSignalsSource())}


TRACE_FORMATS = frozenset(sources())
