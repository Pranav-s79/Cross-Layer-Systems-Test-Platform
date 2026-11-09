"""Adapter for RV32I RTL: the PicoRV32 core simulated with Icarus Verilog.

Lifecycle:

* build:     assemble the program, apply any RTL mutant to a copy of
             ``picorv32.v``, and compile the testbench with ``iverilog``
* execute:   run the compiled simulation with ``vvp`` under a cycle limit
* collect:   keep the trace, the program (source, hex, listing), the mutant
             diff, and optionally a waveform
* normalize: parse one ``RET`` line per retired instruction (from the core's
             RISC-V Formal Interface) into events, and annotate each with what
             the reference ISA model (``iss.py``) retired at the same position

It can also analyze an existing trace (``inputs:``); the program parameter is
still needed so the reference model can run.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any, Mapping, Sequence

from crosslayer.core.adapters.base import (
    AdapterDescription,
    Artifact,
    BuildResult,
    Capability,
    EventSpec,
    ExecutionResult,
    ExecutionStatus,
    FaultRequest,
    FaultSpec,
    InterfaceSpec,
    NormalizationResult,
    ParseDiagnostic,
    RunContext,
    SystemAdapter,
)
from crosslayer.core.contracts.loader import load_contracts_file
from crosslayer.core.contracts.model import BoundaryContract
from crosslayer.core.errors import AdapterError, ConfigError
from crosslayer.core.orchestrator.process import run_process
from crosslayer.core.telemetry.events import NormalizedEvent, Severity
from crosslayer.integrations.rv32i.asm import AsmError, Program, assemble
from crosslayer.integrations.rv32i.iss import EBREAK, ModelRun, Model, decode, lanes

HERE = Path(__file__).resolve().parent
RTL_DIR = HERE / "rtl"
CORE_RTL = RTL_DIR / "picorv32.v"
TESTBENCH = RTL_DIR / "tb.v"
PROGRAMS_DIR = HERE / "programs"
CONTRACTS_PATH = HERE / "contracts.yaml"
SOURCE_SYSTEM = "rv32i"
CLOCK_PERIOD_NS = 10

TRACE_NAME = "trace.log"
STDERR_NAME = "sim.stderr.log"
PROCESS_NAME = "process.json"
PROGRAM_NAME = "program.s"
HEX_NAME = "program.hex"
LISTING_NAME = "program.lst"
DIFF_NAME = "rtl.diff"
WAVE_NAME = "waveform.vcd"

_DEFAULTS: dict[str, Any] = {
    "program": "selftest",
    "max_cycles": 100_000,
    "waveform": False,
    "random_instructions": 300,
}
RANDOM_PROGRAM = "random"
"""``program: random`` generates a program from the experiment seed (``randprog.py``)."""
_SENTINEL = -1
"""Reference value when the model retired nothing at this position: never
equal to a real 32-bit value, so every lockstep check fails there."""

# Each mutant is a set of exact, single-occurrence text edits to picorv32.v,
# or a define PicoRV32 itself provides for testing its verification flow.
_MUTANTS: dict[str, dict[str, Any]] = {
    "bne_inverted": {
        "edits": [("\t\t\tinstr_bne:\n\t\t\t\talu_out_0 = !alu_eq;", "\t\t\tinstr_bne:\n\t\t\t\talu_out_0 = alu_eq;")],
    },
    "sub_as_add": {
        "edits": [("alu_add_sub = instr_sub ? reg_op1 - reg_op2 : reg_op1 + reg_op2;", "alu_add_sub = reg_op1 + reg_op2;")],
    },
    "slt_unsigned": {
        "edits": [("alu_lts = $signed(reg_op1) < $signed(reg_op2);", "alu_lts = reg_op1 < reg_op2;")],
    },
    "jal_imm_bit11": {
        "edits": [("reg_next_pc <= current_pc + decoded_imm_j;",
                   "reg_next_pc <= current_pc + (decoded_imm_j & ~32'h800);")],
    },
    "regfile_wrong_rd": {"defines": ["PICORV32_TESTBUG_001"]},
    "store_byte_lane": {
        "edits": [("mem_la_wstrb = 4'b0001 << reg_op1[1:0];", "mem_la_wstrb = 4'b0001;")],
    },
}

_RET_FIELDS = ("cyc", "ord", "pc", "insn", "rs1", "rs1v", "rs2", "rs2v", "rd", "rdv", "npc", "maddr", "wmask", "wdata", "trap")
_DECIMAL = {"cyc", "ord", "rs1", "rs2", "rd", "trap", "retired", "max_cycles", "wait_states"}
_LINE_RE = re.compile(r"^(?P<tag>[A-Z]+)(?P<rest>(?:\s+[a-z0-9_]+=[0-9a-fA-FxXzZ]+)*)\s*$")
_IGNORED = ("VCD info:",)  # simulator chatter, not trace content


def _rtl_digest() -> str:
    h = hashlib.sha256()
    for path in (CORE_RTL, TESTBENCH):
        h.update(path.read_bytes())
    return h.hexdigest()[:12]


def _positive_int(value: Any, location: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigError("must be a positive integer", location=location)
    return value


class Rv32iAdapter(SystemAdapter):
    def describe(self) -> AdapterDescription:
        return AdapterDescription(
            name="rv32i",
            adapter_version="0.1.0",
            system="PicoRV32 RV32I core (YosysHQ), RTL simulated with Icarus Verilog",
            system_version=f"picorv32.v+tb.v sha256:{_rtl_digest()}",
            subsystems=("fetch", "decoder", "regfile", "alu", "lsu", "memory", "mmio", "core", "tb"),
            interfaces=(
                InterfaceSpec("instruction_bus", "memory", "fetch", "instruction fetches on the memory bus"),
                InterfaceSpec("jump_target", "decoder", "fetch", "decoded jump and branch offsets steer the PC"),
                InterfaceSpec("branch_decision", "alu", "fetch", "the branch compare selects the next PC"),
                InterfaceSpec("operand_read", "regfile", "alu", "register read ports feed the ALU"),
                InterfaceSpec("writeback", "alu", "regfile", "results written to the register file"),
                InterfaceSpec("data_bus", "lsu", "memory", "loads and stores on the memory bus"),
                InterfaceSpec("output_port", "lsu", "mmio", "stores to the memory-mapped output port"),
            ),
            description=(
                "Unmodified PicoRV32 RTL in a CrossLayer testbench. Each retired instruction is "
                "compared with a reference RV32I model running the same program."
            ),
            external_dependencies=("Icarus Verilog (iverilog, vvp) on PATH",),
        )

    def capabilities(self) -> frozenset[Capability]:
        return frozenset(
            {Capability.BUILD, Capability.EXECUTE, Capability.COLLECT, Capability.NORMALIZE, Capability.FAULT_INJECTION}
        )

    def faults(self) -> list[FaultSpec]:
        return [
            FaultSpec("bne_inverted", "RTL mutant: BNE branches when its operands are equal.",
                      target_subsystem="alu", target_interface="branch_decision"),
            FaultSpec("sub_as_add", "RTL mutant: SUB adds instead of subtracting.",
                      target_subsystem="alu", target_interface="writeback"),
            FaultSpec("slt_unsigned", "RTL mutant: signed compares (SLT, SLTI, BLT, BGE) compare unsigned.",
                      target_subsystem="alu", target_interface="writeback"),
            FaultSpec("jal_imm_bit11", "RTL mutant: the JAL target ignores bit 11 of the immediate.",
                      target_subsystem="decoder", target_interface="jump_target"),
            FaultSpec("regfile_wrong_rd", "RTL mutant (PicoRV32's own TESTBUG_001): results are written to register rd^1.",
                      target_subsystem="regfile", target_interface="writeback"),
            FaultSpec("store_byte_lane", "RTL mutant: SB always writes byte lane 0 of the word.",
                      target_subsystem="lsu", target_interface="data_bus"),
            FaultSpec("mem_wait_states", "Testbench memory answers `cycles` clock cycles late (timing only).",
                      target_subsystem="memory", target_interface="instruction_bus",
                      parameters={"cycles": "extra cycles per memory access (default 3)"}),
        ]

    def contracts(self) -> list[BoundaryContract]:
        return load_contracts_file(CONTRACTS_PATH)

    def event_catalog(self) -> list[EventSpec]:
        retire = (
            "rv.cycle", "rv.order", "rv.pc", "rv.insn", "rv.op", "rv.kind", "rv.line", "rv.target", "rv.cpi",
            "rv.rs1", "rv.rs1_val", "rv.rs2", "rv.rs2_val", "rv.rd", "rv.rd_val", "rv.next_pc",
            "rv.mem_addr", "rv.mem_wmask", "rv.mem_wdata", "rv.trap",
            "rv.ref_pc", "rv.ref_insn", "rv.ref_rs1_val", "rv.ref_rs2_val", "rv.ref_rd", "rv.ref_rd_val",
            "rv.ref_next_pc", "rv.ref_mem_addr", "rv.ref_mem_wmask", "rv.ref_mem_wdata", "rv.ref_trap",
        )
        return [
            EventSpec("cpu.boot", ("tb",), ("rv.max_cycles", "rv.wait_states")),
            EventSpec("cpu.retire", ("core",), retire, ("retire",),
                      description="one retired instruction (RVFI) with the reference model's values"),
            EventSpec("cpu.output", ("mmio",), ("rv.cycle", "rv.index", "rv.data", "rv.ref_data")),
            EventSpec("cpu.bus_error", ("memory",), ("rv.cycle", "rv.addr")),
            EventSpec("cpu.halt", ("tb",),
                      ("rv.cycle", "rv.retired", "rv.ref_retired", "rv.halt_reason", "rv.ref_halt_reason")),
            EventSpec("cpu.timeout", ("tb",), ("rv.cycle", "rv.retired")),
        ]

    # -- configuration ------------------------------------------------------------------

    def validate_parameters(self, parameters: Mapping[str, Any]) -> None:
        unknown = set(parameters) - set(_DEFAULTS)
        if unknown:
            raise ConfigError(f"unknown parameters {sorted(unknown)} (allowed: {sorted(_DEFAULTS)})")
        program = parameters.get("program", _DEFAULTS["program"])
        if not isinstance(program, str) or not program:
            raise ConfigError("must be a built-in program name or a path to a .s file", location="parameters.program")
        builtin_ok = program == RANDOM_PROGRAM or (PROGRAMS_DIR / f"{program}.s").is_file()
        if not program.endswith(".s") and not builtin_ok:
            builtin = sorted([p.stem for p in PROGRAMS_DIR.glob("*.s")] + [RANDOM_PROGRAM])
            raise ConfigError(f"unknown program {program!r} (built-in: {builtin}, or give a .s path)",
                              location="parameters.program")
        for key in ("max_cycles", "random_instructions"):
            if key in parameters:
                _positive_int(parameters[key], f"parameters.{key}")
        if not isinstance(parameters.get("waveform", False), bool):
            raise ConfigError("must be true or false", location="parameters.waveform")

    def validate_faults(self, faults: Sequence[FaultRequest]) -> None:
        for fault in faults:
            for param, value in fault.params.items():
                cycles = _positive_int(value, f"faults.{fault.name}.{param}")
                if cycles > 100:
                    raise ConfigError("must be at most 100", location=f"faults.{fault.name}.{param}")

    def _param(self, ctx: RunContext, name: str) -> Any:
        return ctx.parameters.get(name, _DEFAULTS[name])

    def _program_path(self, ctx: RunContext) -> Path:
        program = self._param(ctx, "program")
        path = ctx.resolve(program) if program.endswith(".s") else PROGRAMS_DIR / f"{program}.s"
        if not path.is_file():
            raise AdapterError(f"program not found: {path}")
        return path

    def _program(self, ctx: RunContext) -> tuple[str, Program]:
        if self._param(ctx, "program") == RANDOM_PROGRAM:
            from crosslayer.integrations.rv32i.randprog import generate

            source = generate(ctx.seed, instructions=self._param(ctx, "random_instructions"))
        else:
            source = self._program_path(ctx).read_text(encoding="utf-8")
        try:
            return source, assemble(source)
        except AsmError as exc:
            raise AdapterError(f"cannot assemble {PROGRAM_NAME}: {exc}") from None

    def _write_program(self, ctx: RunContext, source: str, program: Program) -> None:
        (ctx.raw_dir / PROGRAM_NAME).write_text(source, encoding="utf-8", newline="\n")
        (ctx.raw_dir / HEX_NAME).write_text(program.hex_lines(), encoding="utf-8", newline="\n")
        lines = source.splitlines()
        listing = [
            f"{addr:08x}: {word:08x}  {lines[program.source_lines[addr] - 1].strip()}"
            if addr in program.source_lines else f"{addr:08x}: {word:08x}"
            for addr, word in ((4 * i, w) for i, w in enumerate(program.words))
        ]
        (ctx.raw_dir / LISTING_NAME).write_text("\n".join(listing) + "\n", encoding="utf-8", newline="\n")

    def _wait_states(self, ctx: RunContext) -> int:
        for fault in ctx.faults:
            if fault.name == "mem_wait_states":
                return int(fault.params.get("cycles", 3))
        return 0

    # -- lifecycle ----------------------------------------------------------------------

    def build(self, ctx: RunContext) -> BuildResult:
        iverilog = shutil.which("iverilog")
        if iverilog is None:
            return BuildResult(ok=False, details={"error": "iverilog not found on PATH (install Icarus Verilog)"})
        source, program = self._program(ctx)
        self._write_program(ctx, source, program)
        shutil.copyfile(ctx.raw_dir / HEX_NAME, ctx.work_dir / HEX_NAME)

        original = CORE_RTL.read_text(encoding="utf-8")
        rtl, defines, applied = original, ["RISCV_FORMAL"], []
        for fault in ctx.faults:
            mutant = _MUTANTS.get(fault.name)
            if mutant is None:
                continue
            for old, new in mutant.get("edits", []):
                found = rtl.count(old)
                if found != 1:
                    raise AdapterError(f"mutant {fault.name!r} no longer applies to picorv32.v "
                                       f"(expected 1 match, found {found})")
                rtl = rtl.replace(old, new)
            defines += mutant.get("defines", [])
            applied.append(fault.name)
        core = ctx.work_dir / "picorv32.v"
        core.write_text(rtl, encoding="utf-8", newline="\n")
        if applied:
            diff = difflib.unified_diff(
                original.splitlines(keepends=True), rtl.splitlines(keepends=True),
                "rtl/picorv32.v", "rtl/picorv32.v (mutant)",
            )
            text = "".join(diff) + "".join(f"# compiled with -D{d}\n" for d in defines[1:])
            (ctx.raw_dir / DIFF_NAME).write_text(f"# mutants: {', '.join(applied)}\n{text}", encoding="utf-8",
                                                 newline="\n")

        command = [iverilog, "-g2005", *(f"-D{d}" for d in defines), "-o", str(ctx.work_dir / "sim.vvp"),
                   str(TESTBENCH), str(core)]
        log = ctx.work_dir / "iverilog.log"
        result = run_process(command, cwd=ctx.work_dir, timeout_s=ctx.timeout_s, stdout_path=log, stderr_path=log)
        details: dict[str, Any] = {"command": command, "mutants": applied, "program_words": len(program.words)}
        if result.error is not None or result.timed_out or result.exit_code != 0:
            details["error"] = result.error or ("timeout" if result.timed_out else f"exit code {result.exit_code}")
            details["log"] = log.read_text(encoding="utf-8", errors="replace")[-2000:] if log.is_file() else ""
            return BuildResult(ok=False, details=details)
        return BuildResult(ok=True, details=details)

    def execute(self, ctx: RunContext) -> ExecutionResult:
        vvp = shutil.which("vvp") or "vvp"
        words = sum(1 for _ in (ctx.work_dir / HEX_NAME).open(encoding="utf-8"))
        command = [vvp, "-n", "sim.vvp", f"+hex={HEX_NAME}", f"+words={words}",
                   f"+max_cycles={self._param(ctx, 'max_cycles')}", f"+wait_states={self._wait_states(ctx)}"]
        if self._param(ctx, "waveform"):
            command.append(f"+vcd={WAVE_NAME}")
        result = run_process(
            command,
            cwd=ctx.work_dir,
            timeout_s=ctx.timeout_s,
            stdout_path=ctx.raw_dir / TRACE_NAME,
            stderr_path=ctx.raw_dir / STDERR_NAME,
        )
        if result.error is not None:
            status = ExecutionStatus.FAILED_TO_START
        elif result.timed_out:
            status = ExecutionStatus.TIMEOUT
        else:
            status = ExecutionStatus.COMPLETED
        return ExecutionResult(
            status=status,
            exit_code=result.exit_code,
            duration_s=result.duration_s,
            command=command,
            details={"error": result.error} if result.error else {},
        )

    def collect(self, ctx: RunContext, execution: ExecutionResult | None) -> list[Artifact]:
        trace = ctx.raw_dir / TRACE_NAME
        if ctx.inputs:
            if len(ctx.inputs) != 1:
                raise AdapterError("analyze mode expects exactly one trace.log input")
            shutil.copyfile(ctx.inputs[0], trace)
        if not trace.is_file():
            raise AdapterError(f"no {TRACE_NAME} was produced")
        if not (ctx.raw_dir / PROGRAM_NAME).is_file():
            self._write_program(ctx, *self._program(ctx))

        artifacts = [
            Artifact(TRACE_NAME, trace, description="RVFI trace printed by the testbench"),
            Artifact(PROGRAM_NAME, ctx.raw_dir / PROGRAM_NAME, description="program source"),
            Artifact(HEX_NAME, ctx.raw_dir / HEX_NAME, description="program image loaded into memory"),
            Artifact(LISTING_NAME, ctx.raw_dir / LISTING_NAME, description="address, word, and source line"),
        ]
        optional = [
            (STDERR_NAME, "text/plain", "simulator stderr"),
            (DIFF_NAME, "text/x-diff", "RTL changes made by the injected mutant"),
        ]
        wave = ctx.work_dir / WAVE_NAME
        if wave.is_file():
            shutil.move(str(wave), ctx.raw_dir / WAVE_NAME)
            optional.append((WAVE_NAME, "text/plain", "waveform (VCD) of the testbench and core top level"))
        for name, media_type, description in optional:
            path = ctx.raw_dir / name
            if path.is_file():
                artifacts.append(Artifact(name, path, media_type, description))
        if execution is not None:
            record = ctx.raw_dir / PROCESS_NAME
            record.write_text(json.dumps(execution.to_dict(), indent=2) + "\n", encoding="utf-8")
            artifacts.append(Artifact(PROCESS_NAME, record, "application/json", "simulator process record"))
        return artifacts

    def normalize(self, ctx: RunContext, artifacts: Sequence[Artifact]) -> NormalizationResult:
        by_name = {a.name: a for a in artifacts}
        _, program = self._program(ctx)
        reference = Model(program.image()).run(max_steps=self._param(ctx, "max_cycles"))
        parser = _TraceParser(ctx, program, reference)
        with by_name[TRACE_NAME].path.open("r", encoding="utf-8") as fh:
            for lineno, raw in enumerate(fh, start=1):
                parser.feed(lineno, raw.rstrip("\r\n"))
        return NormalizationResult(
            events=parser.events,
            diagnostics=parser.diagnostics,
            stats={
                "trace_lines": parser.lines,
                "events": len(parser.events),
                "retired": parser.retired,
                "reference_retired": len(reference.retires),
                "reference_halt": reference.halt,
                "diagnostics": len(parser.diagnostics),
            },
        )


class _TraceParser:
    def __init__(self, ctx: RunContext, program: Program, reference: ModelRun) -> None:
        self.ctx = ctx
        self.program = program
        self.reference = reference
        self.events: list[NormalizedEvent] = []
        self.diagnostics: list[ParseDiagnostic] = []
        self.lines = 0
        self.retired = 0
        self.outputs = 0
        self.last_cycle = 0
        self.last_retire: dict[str, int] | None = None

    def feed(self, lineno: int, line: str) -> None:
        if not line.strip() or line.startswith(_IGNORED):
            return
        self.lines += 1
        match = _LINE_RE.match(line)
        if match is None:
            self._diag(lineno, f"unrecognized line: {line[:120]!r}")
            return
        try:
            fields = {k: (int(v) if k in _DECIMAL else int(v, 16))
                      for k, v in (kv.split("=", 1) for kv in match["rest"].split())}
        except ValueError:
            self._diag(lineno, f"non-numeric value (X or Z?) in: {line[:120]!r}")
            return
        handler = getattr(self, f"_on_{match['tag'].lower()}", None)
        if handler is None:
            self._diag(lineno, f"unknown record {match['tag']}")
            return
        missing = [k for k in _REQUIRED.get(match["tag"], ()) if k not in fields]
        if missing:
            self._diag(lineno, f"{match['tag']} line missing {missing}")
            return
        handler(lineno, fields)

    def _diag(self, lineno: int, message: str) -> None:
        self.diagnostics.append(ParseDiagnostic(TRACE_NAME, lineno, message))

    def _event(self, lineno: int, cycle: int, event_type: str, subsystem: str, attributes: dict[str, Any],
               severity: Severity = Severity.INFO, correlation: dict[str, str] | None = None) -> None:
        self.events.append(
            NormalizedEvent(
                run_id=self.ctx.run_id,
                event_id=f"{TRACE_NAME}:{lineno}",
                source_system=SOURCE_SYSTEM,
                subsystem=subsystem,
                timestamp_ns=cycle * CLOCK_PERIOD_NS,
                event_type=event_type,
                severity=severity,
                correlation_ids=correlation or {},
                attributes=attributes,
                raw_artifact_ref=self.ctx.raw_ref(TRACE_NAME, lineno),
                confidence_metadata={"timestamp_source": "rtl_cycle_count", "clock_period_ns": CLOCK_PERIOD_NS},
            )
        )

    def _on_boot(self, lineno: int, f: dict[str, int]) -> None:
        self._event(lineno, 0, "cpu.boot", "tb", {"rv.max_cycles": f.get("max_cycles"),
                                                  "rv.wait_states": f.get("wait_states")})

    def _on_ret(self, lineno: int, f: dict[str, int]) -> None:
        d = decode(f["insn"])
        order = self.retired
        self.retired += 1
        attrs: dict[str, Any] = {
            "rv.cycle": f["cyc"], "rv.order": f["ord"], "rv.pc": f["pc"], "rv.insn": f["insn"],
            "rv.op": d.mnemonic, "rv.kind": d.kind, "rv.cpi": f["cyc"] - self.last_cycle,
            "rv.rs1": f["rs1"], "rv.rs1_val": f["rs1v"], "rv.rs2": f["rs2"], "rv.rs2_val": f["rs2v"],
            "rv.rd": f["rd"], "rv.rd_val": f["rdv"], "rv.next_pc": f["npc"],
            "rv.mem_addr": f["maddr"], "rv.mem_wmask": f["wmask"], "rv.mem_wdata": f["wdata"] & lanes(f["wmask"]),
            "rv.trap": f["trap"],
        }
        if d.kind in ("branch", "jal"):
            attrs["rv.target"] = (f["pc"] + d.imm) & 0xFFFF_FFFF
        if f["pc"] in self.program.source_lines:
            attrs["rv.line"] = self.program.source_lines[f["pc"]]
        ref = self.reference.retires[order] if order < len(self.reference.retires) else None
        attrs.update({
            "rv.ref_pc": ref.pc if ref else _SENTINEL,
            "rv.ref_insn": ref.insn if ref else _SENTINEL,
            "rv.ref_rs1_val": ref.rs1_val if ref else _SENTINEL,
            "rv.ref_rs2_val": ref.rs2_val if ref else _SENTINEL,
            "rv.ref_rd": ref.rd if ref else _SENTINEL,
            "rv.ref_rd_val": ref.rd_val if ref else _SENTINEL,
            "rv.ref_next_pc": ref.next_pc if ref else _SENTINEL,
            "rv.ref_mem_addr": ref.mem_addr if ref else _SENTINEL,
            "rv.ref_mem_wmask": ref.mem_wmask if ref else _SENTINEL,
            "rv.ref_mem_wdata": ref.mem_wdata if ref else _SENTINEL,
            "rv.ref_trap": int(ref.trap) if ref else _SENTINEL,
        })
        self.last_cycle = f["cyc"]
        self.last_retire = f
        self._event(lineno, f["cyc"], "cpu.retire", "core", attrs,
                    Severity.WARNING if f["trap"] else Severity.INFO, {"retire": str(order)})

    def _on_out(self, lineno: int, f: dict[str, int]) -> None:
        index = self.outputs
        self.outputs += 1
        expected = self.reference.outputs[index] if index < len(self.reference.outputs) else _SENTINEL
        self._event(lineno, f["cyc"], "cpu.output", "mmio",
                    {"rv.cycle": f["cyc"], "rv.index": index, "rv.data": f["data"], "rv.ref_data": expected})

    def _on_buserr(self, lineno: int, f: dict[str, int]) -> None:
        self._event(lineno, f["cyc"], "cpu.bus_error", "memory", {"rv.cycle": f["cyc"], "rv.addr": f["addr"]},
                    Severity.ERROR)

    def _on_halt(self, lineno: int, f: dict[str, int]) -> None:
        last = self.last_retire
        if last is not None and last["insn"] == EBREAK:
            reason = "ebreak"
        elif last is not None and decode(last["insn"]).mnemonic == "ecall":
            reason = "ecall"
        else:
            reason = "trap"
        self._event(lineno, f["cyc"], "cpu.halt", "tb", {
            "rv.cycle": f["cyc"], "rv.retired": f["retired"], "rv.ref_retired": len(self.reference.retires),
            "rv.halt_reason": reason, "rv.ref_halt_reason": self.reference.halt,
        })

    def _on_timeout(self, lineno: int, f: dict[str, int]) -> None:
        self._event(lineno, f["cyc"], "cpu.timeout", "tb", {"rv.cycle": f["cyc"], "rv.retired": f["retired"]},
                    Severity.ERROR)


_REQUIRED: dict[str, tuple[str, ...]] = {
    "RET": _RET_FIELDS,
    "OUT": ("cyc", "data"),
    "BUSERR": ("cyc", "addr"),
    "HALT": ("cyc", "retired"),
    "TIMEOUT": ("cyc", "retired"),
}
