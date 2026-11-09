"""RV32I adapter, assembler, reference model, and contract integration tests."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from crosslayer.core.adapters import Artifact, FaultRequest, RunContext, check_adapter_conformance
from crosslayer.core.contracts.evaluator import evaluate_contracts
from crosslayer.core.contracts.testing import run_contract_tests
from crosslayer.core.errors import ConfigError
from crosslayer.core.orchestrator import ExperimentRunner, load_experiment
from crosslayer.core.telemetry import read_events_jsonl
from crosslayer.core.verdict import Verdict
from crosslayer.integrations.rv32i.adapter import Rv32iAdapter
from crosslayer.integrations.rv32i.asm import AsmError, Program, assemble
from crosslayer.integrations.rv32i.iss import MODEL_FAULTS, OUT_ADDR, Model, ModelRun, first_divergence
from crosslayer.integrations.rv32i.randprog import generate

PACKAGE = Path(__file__).resolve().parents[2] / "crosslayer" / "integrations" / "rv32i"
EXPERIMENTS = Path(__file__).resolve().parents[2] / "experiments" / "rv32i"
SELFTEST_SOURCE = (PACKAGE / "programs" / "selftest.s").read_text(encoding="utf-8")
HAS_ICARUS = shutil.which("iverilog") is not None and shutil.which("vvp") is not None


def _context(tmp_path: Path, *, parameters: dict[str, object] | None = None) -> RunContext:
    raw_dir = tmp_path / "raw"
    work_dir = tmp_path / "work"
    raw_dir.mkdir()
    work_dir.mkdir()
    return RunContext(
        run_id="rv32i-test",
        seed=7,
        parameters=parameters or {"program": "selftest", "max_cycles": 100_000},
        faults=(),
        raw_dir=raw_dir,
        work_dir=work_dir,
        base_dir=tmp_path,
        timeout_s=30,
    )


def _trace(run: ModelRun, *, cycles_per_instruction: int = 4) -> str:
    lines = ["BOOT max_cycles=100000 wait_states=0"]
    for retire in run.retires:
        cycle = (retire.order + 1) * cycles_per_instruction
        lines.append(
            "RET "
            f"cyc={cycle} ord={retire.order} pc={retire.pc:08x} insn={retire.insn:08x} "
            f"rs1={retire.rs1} rs1v={retire.rs1_val:08x} rs2={retire.rs2} rs2v={retire.rs2_val:08x} "
            f"rd={retire.rd} rdv={retire.rd_val:08x} npc={retire.next_pc:08x} "
            f"maddr={retire.mem_addr:08x} wmask={retire.mem_wmask:x} wdata={retire.mem_wdata:08x} "
            f"trap={int(retire.trap)}"
        )
        if retire.mem_addr == OUT_ADDR and retire.mem_wmask:
            lines.append(f"OUT cyc={cycle} data={retire.mem_wdata:08x}")
    final_cycle = len(run.retires) * cycles_per_instruction
    lines.append(f"HALT cyc={final_cycle} retired={len(run.retires)}")
    return "\n".join(lines) + "\n"


def _normalize(tmp_path: Path, trace: str):
    ctx = _context(tmp_path)
    trace_path = ctx.raw_dir / "trace.log"
    trace_path.write_text(trace, encoding="utf-8")
    artifact = Artifact("trace.log", trace_path)
    return Rv32iAdapter().normalize(ctx, [artifact])


def test_adapter_is_conformant_and_contract_cases_have_full_coverage():
    adapter = Rv32iAdapter()
    assert check_adapter_conformance(adapter) == []
    report = run_contract_tests(PACKAGE / "contract_tests.yaml")
    assert report.passed
    assert report.uncovered() == []


def test_selftest_assembles_and_clean_model_reaches_ebreak():
    program = assemble(SELFTEST_SOURCE)
    run = Model(program.image()).run()

    assert len(program.words) > 100
    assert run.halt == "ebreak"
    assert len(run.retires) == 626
    assert len(run.outputs) == 32
    assert run.bus_errors == []
    assert program.source_lines[0] > 0


@pytest.mark.parametrize("fault", sorted(MODEL_FAULTS))
def test_selftest_exposes_every_reference_model_fault(fault: str):
    image = assemble(SELFTEST_SOURCE).image()
    clean = Model(image).run()
    faulty = Model(image, faults={fault}).run()

    divergence = first_divergence(clean.retires, faulty.retires)
    assert divergence is not None
    assert 0 <= divergence < len(clean.retires)


def test_normalized_clean_trace_passes_all_contracts(tmp_path: Path):
    clean = Model(assemble(SELFTEST_SOURCE).image()).run()
    normalized = _normalize(tmp_path, _trace(clean))
    results = evaluate_contracts(Rv32iAdapter().contracts(), normalized.events)

    assert normalized.diagnostics == []
    assert normalized.stats["retired"] == 626
    assert {result.verdict for result in results} == {Verdict.PASS}


@pytest.mark.parametrize("fault", sorted(MODEL_FAULTS))
def test_lockstep_reports_each_mutant_at_its_first_divergence(tmp_path: Path, fault: str):
    image = assemble(SELFTEST_SOURCE).image()
    clean = Model(image).run()
    faulty = Model(image, faults={fault}).run()
    divergence = first_divergence(clean.retires, faulty.retires)
    assert divergence is not None

    normalized = _normalize(tmp_path, _trace(faulty))
    results = evaluate_contracts(Rv32iAdapter().contracts(), normalized.events)
    failures = [result for result in results if result.verdict is Verdict.FAIL]

    assert failures
    assert min(result.first_violation_ns for result in failures if result.first_violation_ns is not None) == (
        divergence + 1
    ) * 40


def test_wait_states_only_trip_the_cpi_contract(tmp_path: Path):
    clean = Model(assemble(SELFTEST_SOURCE).image()).run()
    normalized = _normalize(tmp_path, _trace(clean, cycles_per_instruction=12))
    results = evaluate_contracts(Rv32iAdapter().contracts(), normalized.events)

    failures = [result.contract.id for result in results if result.verdict is Verdict.FAIL]
    assert failures == ["rv32i.cpi_budget"]


def test_parser_records_unknown_and_incomplete_records(tmp_path: Path):
    normalized = _normalize(
        tmp_path,
        "BOOT max_cycles=100 wait_states=0\n"
        "RET cyc=1 ord=0\n"
        "MYSTERY cyc=2\n"
        "not a record\n",
    )

    assert [diagnostic.line for diagnostic in normalized.diagnostics] == [2, 3, 4]
    assert "missing" in normalized.diagnostics[0].message
    assert "unknown record" in normalized.diagnostics[1].message
    assert "unrecognized line" in normalized.diagnostics[2].message


def test_random_programs_are_reproducible_distinct_and_terminating():
    source = generate(1234, instructions=100)
    assert source == generate(1234, instructions=100)
    assert source != generate(1235, instructions=100)

    run = Model(assemble(source).image()).run(max_steps=1000)
    assert run.halt == "ebreak"
    assert run.bus_errors == []
    assert len(run.outputs) == 1


@pytest.mark.parametrize("instructions", [0, -1, True, 1.5])
def test_random_program_rejects_invalid_instruction_counts(instructions: object):
    with pytest.raises((TypeError, ValueError)):
        generate(1, instructions=instructions)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "source, message",
    [
        ("addi x1, x0, 2048", "immediate 2048 out of range"),
        ("beq x0, x0, missing", "unknown label"),
        ("lw x1, 2", r"expected imm\(reg\)"),
        ("mystery x1, x2", "unknown instruction"),
    ],
)
def test_assembler_rejects_invalid_programs(source: str, message: str):
    with pytest.raises(AsmError, match=message):
        assemble(source)


def test_adapter_rejects_unknown_parameters_and_invalid_fault_values():
    adapter = Rv32iAdapter()
    with pytest.raises(ConfigError, match="unknown parameters"):
        adapter.validate_parameters({"mystery": 1})
    with pytest.raises(ConfigError, match="positive integer"):
        adapter.validate_parameters({"max_cycles": 0})
    with pytest.raises(ConfigError, match="at most 100"):
        adapter.validate_faults([FaultRequest("mem_wait_states", {"cycles": 101})])


def test_model_rejects_unknown_faults_and_oversized_images():
    with pytest.raises(ValueError, match="unknown model faults"):
        Model(b"", faults={"unknown"})
    with pytest.raises(ValueError, match="exceeds"):
        Model(b"\x00" * (64 * 1024 + 1))


def test_program_image_is_little_endian():
    program = Program([0x12345678], {}, {0: 1})
    assert program.image() == b"\x78\x56\x34\x12"
    assert program.hex_lines() == "12345678\n"


@pytest.mark.skipif(not HAS_ICARUS, reason="Icarus Verilog is not installed")
def test_iverilog_baseline_passes_every_contract(tmp_path: Path):
    result = ExperimentRunner(tmp_path / "runs").run(load_experiment(EXPERIMENTS / "baseline.yaml"))

    assert result.verdict is Verdict.PASS
    assert {contract.verdict for contract in result.results} == {Verdict.PASS}


@pytest.mark.skipif(not HAS_ICARUS, reason="Icarus Verilog is not installed")
@pytest.mark.parametrize("fault", sorted(MODEL_FAULTS))
def test_iverilog_mutant_first_mismatch_matches_reference_model(tmp_path: Path, fault: str):
    experiment = EXPERIMENTS / f"fault_{fault}.yaml"
    result = ExperimentRunner(tmp_path / "runs").run(load_experiment(experiment))
    events = read_events_jsonl(result.bundle_dir / "normalized_events.jsonl")
    compared_fields = (
        "pc", "insn", "rs1_val", "rs2_val", "rd", "rd_val", "next_pc",
        "mem_addr", "mem_wmask", "mem_wdata", "trap",
    )
    first_mismatch = next(
        event.attributes["rv.order"]
        for event in events
        if event.event_type == "cpu.retire"
        and any(event.attributes[f"rv.{field}"] != event.attributes[f"rv.ref_{field}"] for field in compared_fields)
    )

    image = assemble(SELFTEST_SOURCE).image()
    expected = first_divergence(Model(image).run().retires, Model(image, faults={fault}).run().retires)
    assert result.verdict is Verdict.FAIL
    assert first_mismatch == expected
