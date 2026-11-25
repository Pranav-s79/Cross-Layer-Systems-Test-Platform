"""``trace: commit-log``: Spike-style commit logs (``spike --log-commits``).

One line per committed instruction with its register write and memory
access, plus a line per exception::

    core   0: 3 0x00000010 (0x00a12023) mem 0x00001004 0x0000002a
    core   0: 3 0x00000014 (0x00012283) x5 0x0000002a mem 0x00001004
    core   0: exception trap_breakpoint, epc 0x00000018

This is the format instruction-set simulators and many RTL tracers emit, and
the one random-instruction flows compare. It carries no cycle counts and no
register values read, so ``rv.cpi``, ``rv.rs1_val`` and ``rv.rs2_val`` are
absent and the contracts that need them are SKIPPED. The RVFI fields it does
imply are rebuilt the way RVFI reports them:

* write mask and lane-shifted data from the store width (decoded from the
  instruction) and the byte address; the address becomes word-aligned;
* the next PC from the following commit or exception line;
* an exception's instruction word from the loaded program image.

Testbench markers (``OUT``, ``HALT``...) may be interleaved. Floating-point
and CSR writes are ignored (RV32I has none).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterator

from crosslayer.core.adapters import ParseDiagnostic
from crosslayer.integrations.rv32i.iss import decode
from crosslayer.integrations.rv32i.traces import (
    TRACE_LOG,
    Fetch,
    Item,
    Record,
    TraceSource,
    ignorable,
    parse_marker,
)

_HEX = r"0x([0-9a-fA-F]+)"
_COMMIT_RE = re.compile(rf"^core\s+(?P<hart>\d+):\s+(?P<priv>\d)\s+{_HEX}\s+\({_HEX}\)(?P<rest>.*)$")
_EXCEPTION_RE = re.compile(rf"^core\s+(?P<hart>\d+):\s+exception\s+(?P<name>\w+),\s+epc\s+{_HEX}")
_XWRITE_RE = re.compile(rf"(?<![\w.])x(\d{{1,2}})\s+{_HEX}")
_MEM_RE = re.compile(rf"\bmem\s+{_HEX}(?:\s+{_HEX})?")
_MASK32 = 0xFFFF_FFFF
_STORE_BYTES = {"sb": 1, "sh": 2, "sw": 4}


class CommitLogSource(TraceSource):
    name = "commit-log"
    timed = False
    operands = False
    next_pc_inferred = True

    def plusargs(self) -> list[str]:
        return ["+commit_log"]

    def parse(self, raw_dir: Path, fetch: Fetch) -> Iterator[Item]:
        pending: Record | None = None
        order = 0
        with (raw_dir / TRACE_LOG).open("r", encoding="utf-8") as fh:
            for lineno, raw in enumerate(fh, start=1):
                line = raw.rstrip("\r\n")
                if ignorable(line):
                    continue
                marker = parse_marker(TRACE_LOG, lineno, line)
                if marker is not None:
                    yield marker
                    continue
                commit = _COMMIT_RE.match(line)
                exception = None if commit else _EXCEPTION_RE.match(line)
                match = commit or exception
                if match is None:
                    yield ParseDiagnostic(TRACE_LOG, lineno, f"unrecognized line: {line[:120]!r}")
                    continue
                if match["hart"] != "0":
                    yield ParseDiagnostic(TRACE_LOG, lineno, f"only hart 0 is supported, got hart {match['hart']}")
                    continue
                pc = int(match.group(3), 16) & _MASK32
                if pending is not None:
                    yield _with_next_pc(pending, pc)
                    pending = None
                if exception is not None:
                    yield Record("retire", TRACE_LOG, lineno, {
                        "ord": order, "pc": pc, "insn": fetch(pc), "rd": 0, "rdv": 0,
                        "maddr": 0, "wmask": 0, "wdata": 0, "trap": 1,
                    })
                else:
                    fields = _commit_fields(pc, int(match.group(4), 16) & _MASK32, match["rest"])
                    if isinstance(fields, str):
                        yield ParseDiagnostic(TRACE_LOG, lineno, fields)
                        continue
                    pending = Record("retire", TRACE_LOG, lineno, {"ord": order, **fields})
                order += 1
        if pending is not None:
            yield pending  # the next PC is unknown, so the control-flow check is unresolved here


def _with_next_pc(record: Record, next_pc: int) -> Record:
    return Record(record.kind, record.artifact, record.line, {**record.fields, "npc": next_pc})


def _commit_fields(pc: int, insn: int, rest: str) -> dict[str, int] | str:
    d = decode(insn)
    fields = {"pc": pc, "insn": insn, "rd": 0, "rdv": 0, "maddr": 0, "wmask": 0, "wdata": 0, "trap": 0}
    writes = _XWRITE_RE.findall(rest)
    if len(writes) > 1:
        return f"more than one register write in: {rest.strip()[:80]!r}"
    if writes:
        rd, value = int(writes[0][0]), int(writes[0][1], 16) & _MASK32
        if rd > 31:
            return f"no register x{rd} in RV32I"
        fields.update(rd=rd, rdv=value)
    mem = _MEM_RE.search(rest)
    if mem is not None:
        addr = int(mem.group(1), 16) & _MASK32
        fields["maddr"] = addr & ~3
        if d.kind == "store":
            if mem.group(2) is None:
                return f"store without data: {rest.strip()[:80]!r}"
            size = _STORE_BYTES[d.mnemonic]
            shift = addr & 3
            fields["wmask"] = ((1 << size) - 1) << shift
            fields["wdata"] = ((int(mem.group(2), 16) & ((1 << (8 * size)) - 1)) << (8 * shift)) & _MASK32
    elif d.kind in ("load", "store"):
        return f"{d.mnemonic} without a memory access: {rest.strip()[:80]!r}"
    return fields
