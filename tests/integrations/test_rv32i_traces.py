"""RV32I trace sources: every format yields the same records and the same verdicts."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from crosslayer.core.adapters import Artifact, RunContext
from crosslayer.core.contracts.evaluator import evaluate_contracts
from crosslayer.core.errors import ConfigError
from crosslayer.core.orchestrator import ExperimentRunner
from crosslayer.core.orchestrator.experiment import parse_experiment
from crosslayer.core.telemetry import read_events_jsonl
from crosslayer.core.verdict import Verdict
from crosslayer.integrations.rv32i.adapter import Rv32iAdapter
from crosslayer.integrations.rv32i.asm import assemble
from crosslayer.integrations.rv32i.iss import MODEL_FAULTS, OUT_ADDR, Model, ModelRun, first_divergence
from crosslayer.integrations.rv32i.profile import load_profile
from crosslayer.integrations.rv32i.traces import Record, TraceSource, sources
from crosslayer.integrations.rv32i.traces.commitlog import CommitLogSource

PACKAGE = Path(__file__).resolve().parents[2] / "crosslayer" / "integrations" / "rv32i"
SELFTEST = (PACKAGE / "programs" / "selftest.s").read_text(encoding="utf-8")
HAS_ICARUS = shutil.which("iverilog") is not None and shutil.which("vvp") is not None
SIMULATED_TRACES = ["rvfi", "commit-log", "vcd-signals"]
UNTIMED_SKIPS = {"rv32i.lockstep_operands", "rv32i.cpi_budget"}


SECOND_CORE = "femtorv32-quark"
"""A core with no RVFI port: a testbench monitor and its waveform are the trace."""


def _configured_source(trace: str, core: str = "picorv32") -> TraceSource:
    return sources()[trace].for_profile(load_profile(core).signals)


def _context(tmp_path: Path, **parameters) -> RunContext:
    (tmp_path / "raw").mkdir()
    (tmp_path / "work").mkdir()
    return RunContext(
        run_id="rv32i-trace-test", seed=7, parameters={"program": "selftest", **parameters}, faults=(),
        raw_dir=tmp_path / "raw", work_dir=tmp_path / "work", base_dir=tmp_path, timeout_s=30,
    )


def _commit_log(run: ModelRun) -> str:
    """What the testbench prints in +commit_log mode for this model run."""
    lines = ["BOOT max_cycles=100000 wait_states=0"]
    for r in run.retires:
        if r.trap:
            lines.append(f"core   0: exception trap_breakpoint, epc 0x{r.pc:08x}")
            continue
        line = f"core   0: 3 0x{r.pc:08x} (0x{r.insn:08x})"
        if r.rd:
            line += f" x{r.rd} 0x{r.rd_val:08x}"
        if r.kind in ("load", "store"):  # loads: the word address is all RVFI keeps
            shift = (r.mem_wmask & -r.mem_wmask).bit_length() - 1 if r.mem_wmask else 0
            line += f" mem 0x{r.mem_addr + shift:08x}"
            if r.kind == "store":
                width = bin(r.mem_wmask).count("1")
                line += f" 0x{(r.mem_wdata >> (8 * shift)) & ((1 << (8 * width)) - 1):0{2 * width}x}"
        lines.append(line)
        if r.mem_addr == OUT_ADDR and r.mem_wmask:
            lines.append(f"OUT cyc={r.order} data={r.mem_wdata:08x}")
    lines.append(f"HALT cyc={len(run.retires)} retired={len(run.retires)}")
    return "\n".join(lines) + "\n"


def _normalize(tmp_path: Path, text: str, **parameters):
    ctx = _context(tmp_path, **parameters)
    (ctx.raw_dir / "trace.log").write_text(text, encoding="utf-8")
    return ctx, Rv32iAdapter().normalize(ctx, [Artifact("trace.log", ctx.raw_dir / "trace.log")])


def test_commit_log_of_the_clean_model_passes_every_contract_it_can_evaluate(tmp_path: Path):
    run = Model(assemble(SELFTEST).image()).run()
    _, normalized = _normalize(tmp_path, _commit_log(run), trace="commit-log")
    assert normalized.diagnostics == []
    assert normalized.stats["trace_source"] == "commit-log"
    results = {r.contract.id: r.verdict for r in evaluate_contracts(Rv32iAdapter().contracts(), normalized.events)}
    assert {cid for cid, v in results.items() if v is Verdict.SKIPPED} == UNTIMED_SKIPS
    assert {v for cid, v in results.items() if cid not in UNTIMED_SKIPS} == {Verdict.PASS}


def test_commit_log_rebuilds_rvfi_byte_lanes_and_next_pc(tmp_path: Path):
    # sb x5, 3(x0) at byte address 0x1003; sh x5, 2(x0) at 0x1002; ebreak
    fetch = {0: 0x005001A3, 4: 0x00501123, 8: 0x00100073}.get
    (tmp_path / "trace.log").write_text(
        "core   0: 3 0x00000000 (0x005001a3) mem 0x00001003 0xab\n"
        "core   0: 3 0x00000004 (0x00501123) mem 0x00001002 0xbeef\n"
        "core   0: exception trap_breakpoint, epc 0x00000008\n",
        encoding="utf-8",
    )
    records = [r for r in CommitLogSource().parse(tmp_path, lambda pc: fetch(pc, 0)) if isinstance(r, Record)]
    sb, sh, brk = (r.fields for r in records)
    assert (sb["maddr"], sb["wmask"], sb["wdata"], sb["npc"]) == (0x1000, 0b1000, 0xAB00_0000, 4)
    assert (sh["maddr"], sh["wmask"], sh["wdata"], sh["npc"]) == (0x1000, 0b1100, 0xBEEF_0000, 8)
    assert (brk["pc"], brk["insn"], brk["trap"]) == (8, 0x00100073, 1)
    assert [r.fields["ord"] for r in records] == [0, 1, 2]


@pytest.mark.parametrize(
    "line, message",
    [
        ("core   1: 3 0x00000000 (0x00000013)", "only hart 0"),
        ("core   0: 3 0x00000000 (0x00502023) mem 0x00001000", "store without data"),
        ("core   0: 3 0x00000000 (0x00002283)", "lw without a memory access"),
        ("core   0: 3 0x00000000 (0x00000013) x5 0x00000001 x6 0x00000002", "more than one register write"),
        ("garbage", "unrecognized line"),
    ],
)
def test_commit_log_diagnostics_name_the_problem(tmp_path: Path, line: str, message: str):
    (tmp_path / "trace.log").write_text(line + "\n", encoding="utf-8")
    [item] = list(CommitLogSource().parse(tmp_path, lambda pc: 0))
    assert not isinstance(item, Record)
    assert message in item.message and item.line == 1


def test_trace_must_be_one_the_selected_core_supports(tmp_path: Path):
    adapter = Rv32iAdapter()
    with pytest.raises(ConfigError, match="parameters.trace: must be one of"):
        adapter.validate_parameters({"trace": "jtag"})
    assert set(sources()) >= set(SIMULATED_TRACES)
    root = tmp_path / "rvfi_only"
    shutil.copytree(PACKAGE / "cores" / "picorv32", root)
    profile = (root / "core.yaml").read_text(encoding="utf-8")
    (root / "core.yaml").write_text(profile.replace("trace: [rvfi, commit-log, vcd-signals]", "trace: rvfi"), encoding="utf-8")
    with pytest.raises(ConfigError, match="supports traces \\['rvfi'\\]"):
        adapter.faults_for({"core": str(root), "trace": "commit-log"}, tmp_path)


def _run(tmp_path: Path, trace: str, faults=(), core: str = "picorv32"):
    doc = {"schema_version": 1, "name": f"trace-{trace}", "adapter": "rv32i", "seed": 1, "timeout_s": 120,
           "parameters": {"core": core, "program": "selftest", "trace": trace, "max_cycles": 20_000},
           "faults": list(faults)}
    return ExperimentRunner(tmp_path / "runs").run(parse_experiment(doc, base_dir=tmp_path, source="test"))


@pytest.mark.skipif(not HAS_ICARUS, reason="Icarus Verilog is not installed")
@pytest.mark.parametrize("trace", SIMULATED_TRACES)
def test_clean_core_passes_through_every_trace_source(tmp_path: Path, trace: str):
    result = _run(tmp_path, trace)
    assert result.verdict is Verdict.PASS
    skipped = {r.contract.id for r in result.results if r.verdict is Verdict.SKIPPED}
    assert skipped == (set() if _configured_source(trace).timed else UNTIMED_SKIPS)


@pytest.mark.skipif(not HAS_ICARUS, reason="Icarus Verilog is not installed")
@pytest.mark.parametrize("trace", SIMULATED_TRACES)
@pytest.mark.parametrize("fault", sorted(MODEL_FAULTS))
def test_each_trace_source_finds_the_predicted_first_divergence(tmp_path: Path, trace: str, fault: str):
    _assert_predicted_first_divergence(_run(tmp_path, trace, [fault]), trace, fault)


@pytest.mark.skipif(not HAS_ICARUS, reason="Icarus Verilog is not installed")
@pytest.mark.parametrize("trace", ["vcd-signals", "commit-log"])
def test_second_core_without_rvfi_passes_through_its_trace_sources(tmp_path: Path, trace: str):
    result = _run(tmp_path, trace, core=SECOND_CORE)
    assert result.verdict is Verdict.PASS
    assert result.manifest["build"]["core"]["name"] == SECOND_CORE
    skipped = {r.contract.id for r in result.results if r.verdict is Verdict.SKIPPED}
    assert skipped == (set() if _configured_source(trace, SECOND_CORE).timed else UNTIMED_SKIPS)


@pytest.mark.skipif(not HAS_ICARUS, reason="Icarus Verilog is not installed")
@pytest.mark.parametrize("trace", ["vcd-signals", "commit-log"])
@pytest.mark.parametrize("fault", sorted(MODEL_FAULTS))
def test_second_core_mutants_first_diverge_where_the_model_predicts(tmp_path: Path, trace: str, fault: str):
    _assert_predicted_first_divergence(_run(tmp_path, trace, [fault], core=SECOND_CORE), trace, fault, SECOND_CORE)


def _assert_predicted_first_divergence(result, trace: str, fault: str, core: str = "picorv32") -> None:
    assert result.verdict is Verdict.FAIL
    events = read_events_jsonl(result.bundle_dir / "normalized_events.jsonl")
    fields = ("pc", "insn", "rs1_val", "rs2_val", "rd", "rd_val", "next_pc", "mem_addr", "mem_wmask", "mem_wdata", "trap")
    first = next(
        e.attributes["rv.order"] for e in events
        if e.event_type == "cpu.retire" and any(
            f"rv.{f}" in e.attributes and e.attributes[f"rv.{f}"] != e.attributes[f"rv.ref_{f}"] for f in fields)
    )
    image = assemble(SELFTEST).image()
    observed = [f for f in fields if _configured_source(trace, core).operands or f not in ("rs1_val", "rs2_val")]
    clean, faulty = Model(image).run().retires, Model(image, faults={fault}).run().retires
    assert first == first_divergence(clean, faulty, observed)


VCD_HEADER = """$timescale 1ns $end
$scope module tb $end
$var reg 1 ! clk $end
$var integer 32 " cycle $end
$scope module uut $end
$var wire 1 # valid $end
$var wire 32 $ pc [31:0] $end
$var wire 32 % insn [31:0] $end
$upscope $end
$upscope $end
$enddefinitions $end
"""
SIGNALS = {"clock": "tb.clk", "valid": "tb.uut.valid",
           "fields": {"cyc": "tb.cycle", "pc": "tb.uut.pc", "insn": "tb.uut.insn"}}


def _vcd(tmp_path: Path, body: str) -> list:
    from crosslayer.integrations.rv32i.traces.vcd import WAVEFORM, VcdSignalsSource

    (tmp_path / WAVEFORM).write_text(VCD_HEADER + body, encoding="utf-8")
    (tmp_path / "trace.log").write_text("BOOT max_cycles=10 wait_states=0\nHALT cyc=3 retired=1\n", encoding="utf-8")
    return list(VcdSignalsSource().for_profile(SIGNALS).parse(tmp_path, lambda pc: 0))


def test_vcd_samples_the_values_in_effect_just_before_each_rising_edge(tmp_path: Path):
    # The edge at #10 captures valid=1, pc=4 (set at #5); the changes made AT #10
    # (pc=8, valid=0) belong to the next cycle, as for a flip-flop.
    items = _vcd(tmp_path, "#0\n$dumpvars\n0!\nb0 \"\n0#\nb0 $\nb10011 %\n$end\n"
                           "#5\n1#\nb100 $\nb1 \"\n#10\n1!\nb1000 $\n0#\nb10 \"\n#15\n0!\n#20\n1!\n")
    retires = [i for i in items if isinstance(i, Record) and i.kind == "retire"]
    assert [(r.fields["pc"], r.fields["insn"], r.fields["cyc"], r.line) for r in retires] == [(4, 0b10011, 1, 24)]  # line 24 is the "#10" edge
    assert [i.kind for i in items if isinstance(i, Record)] == ["boot", "retire", "halt"]


def test_vcd_reports_unknown_values_and_missing_signals(tmp_path: Path):
    [unknown] = [i for i in _vcd(tmp_path, "#0\n0!\nb0 \"\n1#\nbx $\nb1 %\n#10\n1!\n") if not isinstance(i, Record)]
    assert "tb.uut.pc is unknown" in unknown.message
    from crosslayer.integrations.rv32i.traces.vcd import VcdSignalsSource

    (tmp_path / "waveform.vcd").write_text(VCD_HEADER, encoding="utf-8")
    missing = VcdSignalsSource().for_profile({**SIGNALS, "valid": "tb.uut.nope"})
    [problem] = [i for i in missing.parse(tmp_path, lambda pc: 0) if not isinstance(i, Record)]
    assert "signals not in the waveform: ['tb.uut.nope']" in problem.message
