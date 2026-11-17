"""``trace: vcd-signals``: retirements sampled from a waveform (VCD).

Any core that can dump a waveform can be verified, with no trace printing in
its RTL or testbench. The core profile maps retirement fields to signals::

    signals:
      clock: tb.clk
      valid: tb.uut.rvfi_valid      # a retirement happens on cycles where this is 1
      fields:
        pc: tb.uut.rvfi_pc_rdata
        insn: tb.uut.rvfi_insn
        ...

At every rising edge of ``clock`` the reader takes the values in effect just
before the edge (what a flip-flop clocked by that edge would capture). If
``valid`` is 1, those values form one retirement. Fields the profile does not
map are absent, so the contracts that need them are SKIPPED. Testbench markers
still come from ``trace.log``; records from both files are merged by cycle.

The reader streams the file line by line, so memory does not grow with the
length of the simulation.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping

from crosslayer.core.adapters import ParseDiagnostic
from crosslayer.integrations.rv32i.traces import (
    TRACE_LOG,
    Fetch,
    Item,
    Record,
    TraceSource,
    ignorable,
    parse_marker,
)

WAVEFORM = "waveform.vcd"


@dataclass(frozen=True)
class SignalMap:
    clock: str
    valid: str
    fields: Mapping[str, str]


class VcdSignalsSource(TraceSource):
    name = "vcd-signals"
    timed = False
    operands = False
    artifacts = (TRACE_LOG, WAVEFORM)

    def __init__(self, signals: SignalMap | None = None) -> None:
        self.signals = signals
        if signals is not None:
            self.timed = "cyc" in signals.fields
            self.operands = {"rs1v", "rs2v"} <= set(signals.fields)

    def for_profile(self, signals: Mapping[str, Any] | None) -> TraceSource:
        if not signals:
            raise ValueError("trace vcd-signals needs a `signals` map in the core profile")
        return VcdSignalsSource(SignalMap(signals["clock"], signals["valid"], dict(signals["fields"])))

    def plusargs(self) -> list[str]:
        return [f"+vcd={WAVEFORM}", "+no_retire_lines"]

    def parse(self, raw_dir: Path, fetch: Fetch) -> Iterator[Item]:
        if self.signals is None:
            raise ValueError("vcd-signals source used without a signal map")
        retires = read_retirements(raw_dir / WAVEFORM, self.signals)
        markers = _markers(raw_dir / TRACE_LOG)
        yield from heapq.merge(retires, markers, key=_cycle)


def _cycle(item: Item) -> int:
    return item.fields.get("cyc", 0) if isinstance(item, Record) else 0


def _markers(path: Path) -> Iterator[Item]:
    """Testbench markers from ``trace.log``; retirement lines there are ignored."""
    with path.open("r", encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, start=1):
            line = raw.rstrip("\r\n")
            if ignorable(line):
                continue
            marker = parse_marker(TRACE_LOG, lineno, line)
            if marker is not None:
                yield marker


def read_retirements(path: Path, signals: SignalMap) -> Iterator[Item]:
    """Yield one retirement per rising clock edge on which ``valid`` was 1."""
    wanted = {"clock": signals.clock, "valid": signals.valid, **signals.fields}
    with path.open("r", encoding="utf-8") as fh:
        lines = enumerate(fh, start=1)
        ids, problem = _header(lines, set(wanted.values()))
        if problem:
            yield ParseDiagnostic(WAVEFORM, 0, problem)
            return
        by_id: dict[str, list[str]] = {}
        for key, name in wanted.items():
            by_id.setdefault(ids[name], []).append(key)
        state: dict[str, str] = {}
        pending: list[tuple[str, str]] = []
        edge_line = 0
        order = 0

        def flush() -> Item | None:
            """Apply the changes at one timestamp; sample first if the clock rose."""
            nonlocal order
            item: Item | None = None
            clock_id = ids[signals.clock]
            rose = state.get(clock_id) == "0" and any(i == clock_id and v == "1" for i, v in pending)
            if rose and _bit(state.get(ids[signals.valid])) == 1:
                item = _sample(state, ids, signals, edge_line, order)
                if isinstance(item, Record):
                    order += 1
            for ident, value in pending:
                state[ident] = value
            pending.clear()
            return item

        for lineno, raw in lines:
            line = raw.strip()
            if not line:
                continue
            if line[0] == "#":
                item = flush()
                if item is not None:
                    yield item
                edge_line = lineno
                continue
            if line[0] == "$":  # $dumpvars / $dumpall / $end wrappers around value changes
                continue
            change = _change(line)
            if change is not None and change[0] in by_id:
                pending.append(change)
        item = flush()
        if item is not None:
            yield item


def _header(lines: Iterator[tuple[int, str]], wanted: set[str]) -> tuple[dict[str, str], str | None]:
    """Map each wanted hierarchical signal name to its VCD identifier code."""
    scope: list[str] = []
    ids: dict[str, str] = {}
    tokens: list[str] = []
    for _, raw in lines:
        tokens += raw.split()
        while "$end" in tokens:
            end = tokens.index("$end")
            command, tokens = tokens[:end], tokens[end + 1:]
            if not command:
                continue
            if command[0] == "$scope" and len(command) >= 3:
                scope.append(command[2])
            elif command[0] == "$upscope" and scope:
                scope.pop()
            elif command[0] == "$var" and len(command) >= 5:
                name = ".".join([*scope, command[4]])
                if name in wanted:
                    ids[name] = command[3]
            elif command[0] == "$enddefinitions":
                missing = sorted(wanted - set(ids))
                return ids, (f"signals not in the waveform: {missing}" if missing else None)
    return ids, "no $enddefinitions in the waveform header"


def _change(line: str) -> tuple[str, str] | None:
    """A value change as (identifier, value bits), or None for real numbers and keywords."""
    if line[0] in "bB":
        value, _, ident = line[1:].partition(" ")
        return ident.strip(), value.lower()
    if line[0] in "01xXzZ":
        return line[1:].strip(), line[0].lower()
    return None


def _bit(value: str | None) -> int | None:
    if value is None or any(c in "xz" for c in value):
        return None
    return int(value, 2)


def _sample(state: Mapping[str, str], ids: Mapping[str, str], signals: SignalMap, line: int, order: int) -> Item:
    fields: dict[str, int] = {"ord": order}
    for key, name in signals.fields.items():
        value = _bit(state.get(ids[name]))
        if value is None:
            return ParseDiagnostic(WAVEFORM, line, f"{name} is unknown (x/z or never set) on a retiring edge")
        fields[key] = value
    return Record("retire", WAVEFORM, line, fields)
