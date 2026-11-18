"""Adapter for RV32I RTL: the PicoRV32 core simulated with Icarus Verilog.

Lifecycle:

* build:     assemble the program, load the selected core profile
             (``cores/<name>/core.yaml``), apply any RTL mutant to run-local
             copies of its sources, and compile the testbench with ``iverilog``
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
import json
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
from crosslayer.core.telemetry.store import order_events
from crosslayer.integrations.rv32i import simcache
from crosslayer.integrations.rv32i.asm import AsmError, Program, assemble
from crosslayer.integrations.rv32i.iss import (
    ACCESS_BYTES,
    BRANCH_MNEMONICS,
    Decoded,
    EBREAK,
    MNEMONICS,
    MODEL_FAULTS,
    Model,
    ModelRun,
    decode,
    lanes,
)
from crosslayer.integrations.rv32i.traces import TRACE_LOG, Record, TraceSource, sources
from crosslayer.integrations.rv32i.profile import (
    DEFAULT_CORE,
    CoreProfile,
    apply_mutants,
    builtin_cores,
    load_profile,
)

HERE = Path(__file__).resolve().parent
PROGRAMS_DIR = HERE / "programs"
CONTRACTS_PATH = HERE / "contracts.yaml"
SOURCE_SYSTEM = "rv32i"

STDERR_NAME = "sim.stderr.log"
PROCESS_NAME = "process.json"
PROGRAM_NAME = "program.s"
HEX_NAME = "program.hex"
LISTING_NAME = "program.lst"
DIFF_NAME = "rtl.diff"
WAVE_NAME = "waveform.vcd"

_DEFAULTS: dict[str, Any] = {
    "core": DEFAULT_CORE,
    "trace": None,
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



def _positive_int(value: Any, location: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigError("must be a positive integer", location=location)
    return value


SUBSYSTEMS = ("fetch", "decoder", "regfile", "alu", "lsu", "memory", "mmio", "core", "tb")
INTERFACES = (
    InterfaceSpec("instruction_bus", "memory", "fetch", "instruction fetches on the memory bus"),
    InterfaceSpec("jump_target", "decoder", "fetch", "decoded jump and branch offsets steer the PC"),
    InterfaceSpec("branch_decision", "alu", "fetch", "the branch compare selects the next PC"),
    InterfaceSpec("operand_read", "regfile", "alu", "register read ports feed the ALU"),
    InterfaceSpec("writeback", "alu", "regfile", "results written to the register file"),
    InterfaceSpec("data_bus", "lsu", "memory", "loads and stores on the memory bus"),
    InterfaceSpec("output_port", "lsu", "mmio", "stores to the memory-mapped output port"),
)
WAIT_STATES_FAULT = FaultSpec(
    "mem_wait_states", "Testbench memory answers `cycles` clock cycles late (timing only).",
    target_subsystem="memory", target_interface="instruction_bus",
    parameters={"cycles": "extra cycles per memory access (default 3)"},
)
"""Applies to every core: it is a testbench plusarg, not an RTL edit."""


def _load(core: str, base_dir: Path | None = None) -> CoreProfile:
    return load_profile(core, base_dir, subsystems=SUBSYSTEMS, interfaces={i.name for i in INTERFACES},
                        model_faults=MODEL_FAULTS)


def _mutant_faults(profile: CoreProfile) -> list[FaultSpec]:
    return [
        FaultSpec(m.name, f"RTL mutant ({profile.name}): {m.description}",
                  target_subsystem=m.target_subsystem, target_interface=m.target_interface)
        for m in profile.mutants.values()
    ]


def _trace_name(profile: CoreProfile, requested: str | None) -> str:
    if requested is None:
        return profile.trace
    if requested not in profile.traces:
        raise ConfigError(f"core {profile.name!r} supports traces {list(profile.traces)}", location="parameters.trace")
    return requested


class Rv32iAdapter(SystemAdapter):
    def describe(self) -> AdapterDescription:
        default = _load(DEFAULT_CORE)
        return AdapterDescription(
            name="rv32i",
            adapter_version="0.2.0",
            system="RV32I cores described by core profiles, RTL simulated with Icarus Verilog",
            system_version=f"default core {default.name} sha256:{default.digest()}",
            subsystems=SUBSYSTEMS,
            interfaces=INTERFACES,
            description=(
                "Unmodified RTL of a selected core (built-in: " + ", ".join(builtin_cores()) + ") in its "
                "profile's testbench. Each retired instruction is compared with a reference RV32I model "
                "running the same program."
            ),
            external_dependencies=("Icarus Verilog (iverilog, vvp) on PATH",),
        )

    def capabilities(self) -> frozenset[Capability]:
        return frozenset(
            {Capability.BUILD, Capability.EXECUTE, Capability.COLLECT, Capability.NORMALIZE, Capability.FAULT_INJECTION}
        )

    def faults(self) -> list[FaultSpec]:
        """Mutants of every built-in core, plus the timing fault."""
        specs: dict[str, FaultSpec] = {}
        for core in builtin_cores():
            for spec in _mutant_faults(_load(core)):
                specs.setdefault(spec.name, spec)
        return [*specs.values(), WAIT_STATES_FAULT]

    def faults_for(self, parameters: Mapping[str, Any], base_dir: Path) -> list[FaultSpec]:
        """Mutants of the selected core only, plus the timing fault."""
        profile = _load(parameters.get("core", DEFAULT_CORE), base_dir)
        _trace_name(profile, parameters.get("trace"))  # an unsupported trace fails at plan time
        return [*_mutant_faults(profile), WAIT_STATES_FAULT]

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
            EventSpec("cpu.boot", ("tb",), ("rv.max_cycles", "rv.wait_states", "rv.trace", "rv.timed", "rv.has_operands"),
                      description="run start; the trace format and whether it carries cycles and operand values"),
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
        core = parameters.get("core", DEFAULT_CORE)
        if not isinstance(core, str) or not core:
            raise ConfigError("must be a built-in core name or a path to a core.yaml", location="parameters.core")
        trace = parameters.get("trace")
        if trace is not None and (not isinstance(trace, str) or trace not in sources()):
            raise ConfigError(f"must be one of {sorted(sources())}", location="parameters.trace")
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

    def _profile(self, ctx: RunContext) -> CoreProfile:
        return _load(self._param(ctx, "core"), ctx.base_dir)

    def _source(self, ctx: RunContext, profile: CoreProfile) -> TraceSource:
        return sources()[_trace_name(profile, self._param(ctx, "trace"))].for_profile(profile.signals)

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

        profile = self._profile(ctx)
        applied = [f.name for f in ctx.faults if f.name in profile.mutants]
        try:
            texts, mutant_defines = apply_mutants(profile, applied)
        except ConfigError as exc:
            raise AdapterError(str(exc)) from None
        diff_lines: list[str] = []
        compiled: list[tuple[str, str]] = []
        for name in (profile.testbench, *profile.sources):
            original = profile.file(name).read_text(encoding="utf-8")
            text = texts.get(name, original)
            target = ctx.work_dir / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8", newline="\n")
            compiled.append((name, text))
            if text != original:
                diff_lines += difflib.unified_diff(
                    original.splitlines(keepends=True), text.splitlines(keepends=True),
                    f"{profile.name}/{name}", f"{profile.name}/{name} (mutant)",
                )
        defines = [*profile.defines, *mutant_defines]
        if applied:
            text = "".join(diff_lines) + "".join(f"# compiled with -D{d}\n" for d in mutant_defines)
            (ctx.raw_dir / DIFF_NAME).write_text(f"# core: {profile.name}\n# mutants: {', '.join(applied)}\n{text}",
                                                 encoding="utf-8", newline="\n")

        flags = ["-g2005", *(f"-D{d}" for d in defines)]
        command = [iverilog, *flags, "-o", "sim.vvp", profile.testbench, *profile.sources]
        details: dict[str, Any] = {"command": command, "core": profile.summary(), "mutants": applied,
                                   "program_words": len(program.words)}
        key = simcache.compile_key([iverilog, shutil.which("vvp")], flags, compiled)
        details["compile_cache"] = {"key": key[:16], "hit": False}
        if simcache.fetch(key, ctx.work_dir / "sim.vvp"):
            details["compile_cache"]["hit"] = True
            return BuildResult(ok=True, details=details)
        stdout_log = ctx.work_dir / "iverilog.stdout.log"
        stderr_log = ctx.work_dir / "iverilog.stderr.log"
        result = run_process(
            command,
            cwd=ctx.work_dir,
            timeout_s=ctx.timeout_s,
            stdout_path=stdout_log,
            stderr_path=stderr_log,
        )
        if result.error is not None or result.timed_out or result.exit_code != 0:
            details["error"] = result.error or ("timeout" if result.timed_out else f"exit code {result.exit_code}")
            logs = [
                f"{path.name}:\n{path.read_text(encoding='utf-8', errors='replace')[-2000:]}"
                for path in (stdout_log, stderr_log)
                if path.is_file() and path.stat().st_size
            ]
            details["log"] = "\n".join(logs)
            return BuildResult(ok=False, details=details)
        simcache.store(key, ctx.work_dir / "sim.vvp")
        return BuildResult(ok=True, details=details)

    def execute(self, ctx: RunContext) -> ExecutionResult:
        vvp = shutil.which("vvp") or "vvp"
        words = sum(1 for _ in (ctx.work_dir / HEX_NAME).open(encoding="utf-8"))
        command = [vvp, "-n", "sim.vvp", f"+hex={HEX_NAME}", f"+words={words}",
                   f"+max_cycles={self._param(ctx, 'max_cycles')}", f"+wait_states={self._wait_states(ctx)}"]
        command += self._source(ctx, self._profile(ctx)).plusargs()
        if self._param(ctx, "waveform") and f"+vcd={WAVE_NAME}" not in command:
            command.append(f"+vcd={WAVE_NAME}")
        result = run_process(
            command,
            cwd=ctx.work_dir,
            timeout_s=ctx.timeout_s,
            stdout_path=ctx.raw_dir / TRACE_LOG,
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
        trace = ctx.raw_dir / TRACE_LOG
        if ctx.inputs:
            if len(ctx.inputs) != 1:
                raise AdapterError("analyze mode expects exactly one trace.log input")
            shutil.copyfile(ctx.inputs[0], trace)
        if not trace.is_file():
            raise AdapterError(f"no {TRACE_LOG} was produced")
        if not (ctx.raw_dir / PROGRAM_NAME).is_file():
            self._write_program(ctx, *self._program(ctx))

        artifacts = [
            Artifact(TRACE_LOG, trace, description="simulator output: testbench markers and trace lines"),
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
        needed = self._source(ctx, self._profile(ctx)).artifacts
        for name in needed:
            if not (ctx.raw_dir / name).is_file():
                raise AdapterError(f"trace source needs {name}, which was not produced")
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
        _, program = self._program(ctx)
        image = program.image()
        reference = Model(image).run(max_steps=self._param(ctx, "max_cycles"))
        profile = self._profile(ctx)
        source = self._source(ctx, profile)

        def fetch(pc: int) -> int:
            return int.from_bytes(image[pc:pc + 4].ljust(4, b"\0"), "little") if 0 <= pc < len(image) else 0

        parser = _TraceParser(ctx, program, reference, profile.clock_period_ns, source)
        for item in source.parse(ctx.raw_dir, fetch):
            parser.feed(item)
        return NormalizationResult(
            events=order_events(parser.events),
            diagnostics=parser.diagnostics,
            stats={
                "trace_source": source.name,
                "trace_records": parser.records,
                "events": len(parser.events),
                "retired": parser.retired,
                "reference_retired": len(reference.retires),
                "reference_halt": reference.halt,
                "diagnostics": len(parser.diagnostics),
                "coverage": parser.coverage(),
            },
        )


_RETIRE_ATTRS = {
    "cyc": "rv.cycle", "ord": "rv.order", "pc": "rv.pc", "insn": "rv.insn",
    "rs1": "rv.rs1", "rs1v": "rv.rs1_val", "rs2": "rv.rs2", "rs2v": "rv.rs2_val",
    "rd": "rv.rd", "rdv": "rv.rd_val", "npc": "rv.next_pc",
    "maddr": "rv.mem_addr", "wmask": "rv.mem_wmask", "wdata": "rv.mem_wdata", "trap": "rv.trap",
}


class _TraceParser:
    """Turns trace records into events, each annotated with the reference model's values."""

    def __init__(self, ctx: RunContext, program: Program, reference: ModelRun, clock_period_ns: int,
                 source: TraceSource) -> None:
        self.ctx = ctx
        self.program = program
        self.reference = reference
        self.clock_period_ns = clock_period_ns
        self.source = source
        self.events: list[NormalizedEvent] = []
        self.diagnostics: list[ParseDiagnostic] = []
        self.records = 0
        self.retired = 0
        self.outputs = 0
        self.last_cycle = 0
        self.last_retire: Mapping[str, int] | None = None
        self.mnemonics = dict.fromkeys(MNEMONICS, 0)
        self.branches = {f"{m}.{way}": 0 for m in BRANCH_MNEMONICS for way in ("taken", "not_taken")}
        self.accesses = {
            f"{m}+{offset}": 0
            for m, size in ACCESS_BYTES.items()
            if m.startswith("s") or source.operands  # a load's byte offset needs its base register value
            for offset in range(0, 4, size)
        }

    def coverage(self) -> dict[str, dict[str, int]]:
        """ISA coverage bins, zero-hit bins included: mnemonics retired, branch
        directions, and each access width at each aligned byte offset."""
        return {"mnemonic": self.mnemonics, "branch": self.branches, "memory": self.accesses}

    def _count(self, f: Mapping[str, int], d: Decoded) -> None:
        if d.mnemonic in self.mnemonics:
            self.mnemonics[d.mnemonic] += 1
        if f.get("trap"):
            return
        if d.kind == "branch" and "npc" in f:
            way = "not_taken" if f["npc"] == (f["pc"] + 4) & 0xFFFF_FFFF else "taken"
            self.branches[f"{d.mnemonic}.{way}"] += 1
        offset = None
        if d.kind == "store" and f.get("wmask"):
            offset = (f["wmask"] & -f["wmask"]).bit_length() - 1
        elif d.kind == "load" and "rs1v" in f:
            offset = (f["rs1v"] + d.imm) & 3
        key = f"{d.mnemonic}+{offset}"
        if key in self.accesses:
            self.accesses[key] += 1

    def feed(self, item: Record | ParseDiagnostic) -> None:
        if isinstance(item, ParseDiagnostic):
            self.diagnostics.append(item)
            return
        self.records += 1
        getattr(self, f"_on_{item.kind}")(item)

    def _event(self, record: Record, event_type: str, subsystem: str, attributes: dict[str, Any],
               severity: Severity = Severity.INFO, correlation: dict[str, str] | None = None) -> None:
        if self.source.timed:
            timestamp = record.fields.get("cyc", 0) * self.clock_period_ns
            timing = {"timestamp_source": "rtl_cycle_count", "clock_period_ns": self.clock_period_ns}
        else:  # no cycles: keep file order (a source may emit a record after the line that completes it)
            timestamp = record.line
            timing = {"timestamp_source": "trace_line"}
        self.events.append(
            NormalizedEvent(
                run_id=self.ctx.run_id,
                event_id=f"{record.artifact}:{record.line}",
                source_system=SOURCE_SYSTEM,
                subsystem=subsystem,
                timestamp_ns=timestamp,
                event_type=event_type,
                severity=severity,
                correlation_ids=correlation or {},
                attributes=attributes,
                raw_artifact_ref=self.ctx.raw_ref(record.artifact, record.line),
                confidence_metadata=timing,
            )
        )

    def _on_boot(self, record: Record) -> None:
        f = record.fields
        self._event(record, "cpu.boot", "tb", {
            "rv.max_cycles": f.get("max_cycles"), "rv.wait_states": f.get("wait_states"),
            "rv.trace": self.source.name, "rv.timed": int(self.source.timed),
            "rv.has_operands": int(self.source.operands),
        })

    def _on_retire(self, record: Record) -> None:
        f = record.fields
        d = decode(f["insn"])
        order = self.retired
        self.retired += 1
        attrs: dict[str, Any] = {name: f[key] for key, name in _RETIRE_ATTRS.items() if key in f}
        attrs.update({"rv.op": d.mnemonic, "rv.kind": d.kind})
        self._count(f, d)
        if "cyc" in f:
            attrs["rv.cpi"] = f["cyc"] - self.last_cycle
            self.last_cycle = f["cyc"]
        if "wdata" in f and "wmask" in f:
            attrs["rv.mem_wdata"] = f["wdata"] & lanes(f["wmask"])
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
        self.last_retire = f
        self._event(record, "cpu.retire", "core", attrs,
                    Severity.WARNING if f.get("trap") else Severity.INFO, {"retire": str(order)})

    def _on_output(self, record: Record) -> None:
        f = record.fields
        index = self.outputs
        self.outputs += 1
        expected = self.reference.outputs[index] if index < len(self.reference.outputs) else _SENTINEL
        self._event(record, "cpu.output", "mmio",
                    {"rv.cycle": f["cyc"], "rv.index": index, "rv.data": f["data"], "rv.ref_data": expected})

    def _on_bus_error(self, record: Record) -> None:
        f = record.fields
        self._event(record, "cpu.bus_error", "memory", {"rv.cycle": f["cyc"], "rv.addr": f["addr"]},
                    Severity.ERROR)

    def _on_halt(self, record: Record) -> None:
        f = record.fields
        last = self.last_retire
        if last is not None and last["insn"] == EBREAK:
            reason = "ebreak"
        elif last is not None and decode(last["insn"]).mnemonic == "ecall":
            reason = "ecall"
        else:
            reason = "trap"
        self._event(record, "cpu.halt", "tb", {
            "rv.cycle": f["cyc"], "rv.retired": f["retired"], "rv.ref_retired": len(self.reference.retires),
            "rv.halt_reason": reason, "rv.ref_halt_reason": self.reference.halt,
        })

    def _on_timeout(self, record: Record) -> None:
        f = record.fields
        self._event(record, "cpu.timeout", "tb", {"rv.cycle": f["cyc"], "rv.retired": f["retired"]},
                    Severity.ERROR)
