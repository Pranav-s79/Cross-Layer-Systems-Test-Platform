"""``trace: rvfi``: the testbench prints one ``RET`` line per RVFI retirement.

The line carries every RVFI field (see the header of ``cores/picorv32/tb.v``),
so all contracts can be evaluated.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

from crosslayer.core.adapters import ParseDiagnostic
from crosslayer.integrations.rv32i.traces import (
    TRACE_LOG,
    Fetch,
    Item,
    Record,
    TraceSource,
    checked,
    ignorable,
    parse_marker,
    parse_tagged,
)


class RvfiSource(TraceSource):
    name = "rvfi"
    timed = True
    operands = True

    def parse(self, raw_dir: Path, fetch: Fetch) -> Iterator[Item]:
        with (raw_dir / TRACE_LOG).open("r", encoding="utf-8") as fh:
            for lineno, raw in enumerate(fh, start=1):
                line = raw.rstrip("\r\n")
                if ignorable(line):
                    continue
                marker = parse_marker(TRACE_LOG, lineno, line)
                if marker is not None:
                    yield marker
                    continue
                parsed = parse_tagged(line)
                if isinstance(parsed, str):
                    yield ParseDiagnostic(TRACE_LOG, lineno, parsed)
                elif parsed[0] != "RET":
                    yield ParseDiagnostic(TRACE_LOG, lineno, f"unknown record {parsed[0]}")
                else:
                    yield checked(Record("retire", TRACE_LOG, lineno, parsed[1]), "RET")
