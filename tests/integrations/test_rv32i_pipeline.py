"""Pipelined RV32I core: hazard faults in the reference model, hazard-aware
normalization and contracts, the third core end to end, and its profile keys."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from crosslayer.core.adapters import Artifact, RunContext
from crosslayer.core.contracts.evaluator import evaluate_contracts
from crosslayer.core.diagnosis import localize
from crosslayer.core.errors import AdapterError, ConfigError
from crosslayer.core.orchestrator import ExperimentRunner
from crosslayer.core.orchestrator.experiment import parse_experiment
from crosslayer.core.telemetry import read_events_jsonl
from crosslayer.core.verdict import Verdict
from crosslayer.integrations.rv32i.adapter import HAZARD_BINS, Rv32iAdapter
from crosslayer.integrations.rv32i.asm import assemble
from crosslayer.integrations.rv32i.iss import (
    PIPELINE_FAULTS,
    Model,
    first_divergence,
    local_effects,
    with_inferred_next_pc,
)
from crosslayer.integrations.rv32i.profile import CORES_DIR, load_profile
from crosslayer.integrations.rv32i.randprog import generate
from crosslayer.integrations.rv32i.traces import sources
from tests.integrations.test_rv32i import _trace

PIPELINE = "learnfpga-pipeline"
SELFTEST = (CORES_DIR.parent / "programs" / "selftest.s").read_text(encoding="utf-8")
HAS_ICARUS = shutil.which("iverilog") is not None and shutil.which("vvp") is not None
LIMIT = 5_000
"""Cycles (and model steps) per faulty run: every fault diverges within the
first 200 retirements, and the clean self-test takes about 880 cycles; two
of the faults loop forever without this."""
FIELDS = ("pc", "insn", "rs1_val", "rs2_val", "rd", "rd_val", "next_pc", "mem_addr", "mem_wmask", "mem_wdata", "trap")


def _model(source: str, *faults: str):
    return Model(assemble(".text\n" + source + "\n    ebreak\n").image(), faults=set(faults)).run()


# -- the reference model's pipeline faults -----------------------------------------


def test_missing_memory_stage_bypass_gives_the_next_instruction_a_stale_operand():
    program = "    addi x5, x0, 1\n    addi x6, x5, 1"
    clean, faulty = _model(program), _model(program, "no_mem_forwarding")
    assert (clean.retires[1].rs1_val, clean.retires[1].rd_val) == (1, 2)
    assert (faulty.retires[1].rs1_val, faulty.retires[1].rd_val) == (0, 1)


def test_missing_write_back_bypass_shows_two_instructions_later_not_one():
    program = "    addi x5, x0, 1\n    addi x6, x5, 1\n    addi x7, x5, 1"
    faulty = _model(program, "no_wb_forwarding").retires
    assert faulty[1].rs1_val == 1  # the memory-stage path still works
    assert faulty[2].rs1_val == 0


def test_the_load_use_stall_hides_a_missing_memory_stage_bypass():
    program = "    li x1, 0x100\n    sb x0, 4(x1)\n    lb x5, 4(x1)\n    add x6, x5, x0"
    # The stall puts a bubble between the load and its use, so the value
    # arrives from write-back: correct even without the memory-stage path.
    assert _model(program, "no_mem_forwarding").retires[-2].rs1_val == 0


def test_without_the_load_use_stall_the_use_gets_the_loads_alu_output():
    program = ("    li x1, 0x100\n    li x2, 0x555\n    sw x2, 12(x1)\n"
               "    lb x5, 12(x1)\n    add x6, x5, x0\n    lbu x7, 13(x1)\n    or x8, x0, x7")
    clean = {r.mnemonic: r for r in _model(program).retires}
    faulty = {r.mnemonic: r for r in _model(program, "no_load_use_stall").retires}
    assert (clean["add"].rs1_val, clean["or"].rs2_val) == (0x55, 0x05)
    assert faulty["add"].rs1_val == 0x100 + 12  # LB's funct3 makes the ALU add: the address
    assert faulty["or"].rs2_val == 0x100 ^ 13  # LBU's funct3 makes it XOR


def test_without_the_branch_flush_the_two_instructions_behind_a_taken_branch_run():
    program = "    beq x0, x0, L\n    addi x5, x0, 1\n    addi x6, x0, 2\n    addi x7, x0, 3\nL:\n    addi x8, x0, 4"
    clean = [r.mnemonic + str(r.rd) for r in _model(program).retires]
    faulty = [r.mnemonic + str(r.rd) for r in _model(program, "no_branch_flush").retires]
    assert clean == ["beq0", "addi8", "ebreak0"]
    assert faulty == ["beq0", "addi5", "addi6", "addi8", "ebreak0"]


def test_a_jump_behind_a_taken_branch_redirects_again_without_the_flush():
    program = ("    beq x0, x0, A\n    jal x0, B\n    addi x5, x0, 1\n    addi x9, x0, 9\n"
               "A:\n    addi x6, x0, 2\n    addi x9, x0, 9\nB:\n    addi x7, x0, 3")
    order = [r.mnemonic + str(r.rd) for r in _model(program, "no_branch_flush").retires]
    # beq -> (jal, addi x5 already fetched) -> A (fetched before jal resolved) -> B
    assert order == ["beq0", "jal0", "addi5", "addi6", "addi7", "ebreak0"]


@pytest.mark.parametrize("fault", sorted(PIPELINE_FAULTS))
def test_selftest_and_hazard_programs_expose_every_pipeline_fault(fault: str):
    for source in (SELFTEST, generate(3, mix="hazards")):
        image = assemble(source).image()
        assert first_divergence(Model(image).run().retires, Model(image, faults={fault}).run().retires) is not None


# -- local effects and observability ------------------------------------------------


def test_local_effects_follow_the_operands_given():
    beq = assemble(".text\n    beq x5, x6, L\n" + "    addi x0, x0, 0\n" * 3 + "L:\n    addi x0, x0, 0\n").words[0]
    assert local_effects(beq, 0x40, 3, 3).next_pc == 0x50
    assert local_effects(beq, 0x40, 3, 4).next_pc == 0x44
    sb = assemble(".text\n    sb x6, 1(x5)\n").words[0]
    effects = local_effects(sb, 0, 0x100, 0xABCD)
    assert (effects.mem_addr, effects.mem_wmask, effects.mem_wdata) == (0x100, 0b0010, 0xCD00)
    lw = assemble(".text\n    lw x7, 0(x5)\n").words[0]
    assert local_effects(lw, 0, 0x100, 0).rd_val is None  # comes from memory
    assert local_effects(lw, 0, 0x102, 0) is None  # misaligned: the reference traps
    assert local_effects(0x0010_0073, 0, 0, 0) is None  # EBREAK


def test_an_inferred_next_pc_shows_a_wrong_path_retirement_at_the_branch():
    program = "    beq x0, x0, L\n    addi x5, x0, 1\n    addi x6, x0, 2\nL:\n    addi x8, x0, 4"
    clean, faulty = _model(program).retires, _model(program, "no_branch_flush").retires
    assert first_divergence(clean, faulty) == 1
    assert first_divergence(with_inferred_next_pc(clean), with_inferred_next_pc(faulty)) == 0
    assert with_inferred_next_pc(faulty)[-1] == faulty[-1]  # the halting trap keeps its own


# -- normalization: dependences, bypass flags, hazard coverage ------------------------


def _context(tmp_path: Path, core: str) -> RunContext:
    (tmp_path / "raw").mkdir()
    (tmp_path / "work").mkdir()
    return RunContext(run_id="pipe", seed=1, parameters={"core": core, "program": "program.s"}, faults=(),
                      raw_dir=tmp_path / "raw", work_dir=tmp_path / "work", base_dir=tmp_path, timeout_s=30)


def _normalized(tmp_path: Path, source: str, core: str = PIPELINE, *faults: str):
    (tmp_path / "program.s").write_text(source, encoding="utf-8")
    ctx = _context(tmp_path, core)
    run = Model(assemble(source).image(), faults=set(faults)).run(max_steps=LIMIT)
    (ctx.raw_dir / "trace.log").write_text(_trace(run), encoding="utf-8")
    return ctx, Rv32iAdapter().normalize(ctx, [Artifact("trace.log", ctx.raw_dir / "trace.log")])


DEPENDENT = """.text
    li x1, 0x100
    lw x5, 0(x1)
    add x6, x5, x0
    addi x7, x6, 1
    sw x7, 4(x1)
    beq x7, x6, done
    addi x9, x7, 0
done:
    ebreak
"""


def test_retirements_carry_producer_distances_and_bypass_flags(tmp_path: Path):
    _, normalized = _normalized(tmp_path, DEPENDENT)
    retires = [e.attributes for e in normalized.events if e.event_type == "cpu.retire"]
    add, addi, sw, beq = retires[2], retires[3], retires[4], retires[5]
    assert (add["rv.rs1_dist"], add["rv.rs1_fwd"], add["rv.rs2_dist"]) == (1, 1, 0)  # x0 has no producer
    assert (sw["rv.rs1_dist"], sw["rv.rs1_fwd"]) == (4, 0)  # x1: outside the bypass window
    assert (sw["rv.rs2_dist"], sw["rv.rs2_fwd"]) == (1, 1)
    assert (beq["rv.rs1_dist"], beq["rv.rs2_dist"]) == (2, 3)
    assert addi["rv.rs1_dist"] == 1
    hazard = normalized.stats["coverage"]["hazard"]
    assert set(hazard) == set(HAZARD_BINS)
    # lw (x1, d1), addi (x6, d1), sw (x7, d1) | beq (x7, d2) | beq (x6, d3), addi x9 (x7, d3)
    assert hazard == {"raw_d1": 3, "raw_d2": 1, "raw_d3": 2, "load_use_d1": 1, "load_use_d2": 0,
                      "branch_dep_d1": 0, "branch_dep_d2": 1, "store_data_d1": 1}


def test_a_core_without_a_bypass_flags_nothing_as_forwarded(tmp_path: Path):
    _, normalized = _normalized(tmp_path, DEPENDENT, "picorv32")
    boot = next(e for e in normalized.events if e.event_type == "cpu.boot").attributes
    assert (boot["rv.pipelined"], boot["rv.bypass_window"], boot["rv.has_next_pc"]) == (0, 0, 1)
    assert not any(e.attributes.get("rv.rs1_fwd") or e.attributes.get("rv.rs2_fwd") for e in normalized.events)


@pytest.mark.parametrize("fault", sorted(PIPELINE_FAULTS))
def test_each_pipeline_fault_is_caught_first_by_a_hazard_contract(tmp_path: Path, fault: str):
    """On a model trace of the fault, the earliest failing contract is a hazard
    contract (or the end-to-end path check that reports the same retirement),
    never the ALU's result check: the ALU computed correctly with what it got."""
    _, normalized = _normalized(tmp_path, SELFTEST, PIPELINE, fault)
    results = evaluate_contracts(Rv32iAdapter().contracts(), normalized.events)
    failing = [r for r in results if r.verdict is Verdict.FAIL]
    first = min(r.first_violation_ns for r in failing)
    earliest = {r.contract.id for r in failing if r.first_violation_ns == first}
    assert earliest & {"rv32i.forwarded_operands", "rv32i.redirect_followed"}
    assert "rv32i.lockstep_writeback" not in earliest


def test_hazard_contracts_are_skipped_on_a_core_that_is_not_pipelined(tmp_path: Path):
    _, normalized = _normalized(tmp_path, SELFTEST, "picorv32")
    results = {r.contract.id: r.verdict for r in evaluate_contracts(Rv32iAdapter().contracts(), normalized.events)}
    assert results["rv32i.forwarded_operands"] is Verdict.SKIPPED
    assert results["rv32i.redirect_followed"] is Verdict.SKIPPED
    assert {v for k, v in results.items() if "forwarded" not in k and "redirect" not in k} == {Verdict.PASS}


# -- structure per configuration ----------------------------------------------------


def test_only_a_pipelined_core_has_hazard_logic_to_blame(tmp_path: Path):
    adapter = Rv32iAdapter()
    assert "hazard" in adapter.describe().subsystems  # the full catalog
    assert "hazard" in adapter.describe_for({"core": PIPELINE}, tmp_path).subsystems
    multicycle = adapter.describe_for({"core": "picorv32"}, tmp_path)
    assert "hazard" not in multicycle.subsystems
    assert all("hazard" not in (i.source, i.destination) for i in multicycle.interfaces)


def test_localization_on_a_multicycle_core_never_names_hazard_logic(tmp_path: Path):
    _, normalized = _normalized(tmp_path, SELFTEST, "picorv32", "regfile_wrong_rd")
    results = evaluate_contracts(Rv32iAdapter().contracts(), normalized.events)
    loc = localize(Rv32iAdapter().describe_for({"core": "picorv32"}, tmp_path), results)
    assert loc.hypotheses[0].subsystem == "regfile"
    assert "hazard" not in [h.subsystem for h in loc.hypotheses]


def test_ground_truth_must_exist_in_the_configured_structure(tmp_path: Path):
    doc = {"schema_version": 1, "name": "gt", "adapter": "rv32i", "seed": 1,
           "parameters": {"core": "picorv32"}, "inputs": ["trace.log"], "ground_truth": {"subsystem": "hazard"}}
    (tmp_path / "trace.log").write_text("", encoding="utf-8")
    with pytest.raises(ConfigError, match="'hazard' is not a subsystem"):
        ExperimentRunner(tmp_path / "runs").plan(parse_experiment(doc, base_dir=tmp_path, source="t"))


# -- profile keys -----------------------------------------------------------------


def test_pipeline_profile_declares_includes_top_images_and_pipeline():
    profile = load_profile(PIPELINE)
    assert profile.top == "tb" and "clockworks.v" in profile.includes
    assert profile.memory_images == {"PROGROM.hex": 16384, "DATARAM.hex": 16384}
    assert profile.pipeline == {"stages": 5, "bypass_window": 2}
    assert {m.target_subsystem for m in profile.mutants.values() if m.model_fault in PIPELINE_FAULTS} == {"hazard"}
    summary = profile.summary()
    assert summary["top"] == "tb" and summary["pipeline"]["bypass_window"] == 2
    assert "top" not in load_profile("picorv32").summary()  # unchanged for profiles without the keys


def _copy(tmp_path: Path, **changes) -> Path:
    root = tmp_path / "core"
    shutil.copytree(CORES_DIR / PIPELINE, root)
    doc = yaml.safe_load((root / "core.yaml").read_text(encoding="utf-8"))
    doc.update(changes)
    (root / "core.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    return root


@pytest.mark.parametrize("changes, match", [
    ({"includes": ["pipeline6.v"]}, "separate from the testbench and sources"),
    ({"includes": ["missing.v"]}, "file not found"),
    ({"top": "tb.uut"}, "must be a Verilog module name"),
    ({"memory_images": {"../rom.hex": 16}}, "invalid file name"),
    ({"memory_images": {"rom.hex": True}}, "word count"),
    ({"pipeline": {"stages": 5, "bypass_window": 5}}, "smaller than stages"),
    ({"pipeline": {"stages": 5}}, "pipeline.bypass_window: must be an integer"),
])
def test_invalid_pipeline_profile_keys_are_rejected(tmp_path: Path, changes, match):
    with pytest.raises(ConfigError, match=match):
        load_profile(str(_copy(tmp_path, **changes)))


def test_a_program_larger_than_a_memory_image_is_refused(tmp_path: Path):
    root = _copy(tmp_path, memory_images={"PROGROM.hex": 4})
    ctx = RunContext(run_id="big", seed=1, parameters={"core": str(root), "program": "selftest"}, faults=(),
                     raw_dir=tmp_path / "raw", work_dir=tmp_path / "work", base_dir=tmp_path, timeout_s=30)
    ctx.raw_dir.mkdir()
    ctx.work_dir.mkdir()
    if shutil.which("iverilog") is None:
        pytest.skip("Icarus Verilog is not installed")
    with pytest.raises(AdapterError, match="does not fit PROGROM.hex"):
        Rv32iAdapter().build(ctx)


def test_random_mix_is_validated_and_hazard_programs_differ():
    with pytest.raises(ConfigError, match="random_mix"):
        Rv32iAdapter().validate_parameters({"program": "random", "random_mix": "chaos"})
    with pytest.raises(ValueError, match="mix must be one of"):
        generate(1, mix="chaos")
    assert generate(5, mix="hazards") == generate(5, mix="hazards") != generate(5)
    run = Model(assemble(generate(5, mix="hazards")).image()).run()
    assert run.halt == "ebreak" and run.bus_errors == [] and len(run.outputs) == 1


def test_hazard_programs_hit_load_use_more_often_than_uniform_ones(tmp_path: Path):
    def load_uses(mix: str, where: Path) -> int:
        where.mkdir()
        return _normalized(where, generate(11, mix=mix))[1].stats["coverage"]["hazard"]["load_use_d1"]

    assert load_uses("hazards", tmp_path / "h") > 3 * load_uses("uniform", tmp_path / "u")


# -- the RTL (Icarus) -------------------------------------------------------------


def _run(tmp_path: Path, trace: str, faults=()):
    doc = {"schema_version": 1, "name": f"pipe-{trace}", "adapter": "rv32i", "seed": 1, "timeout_s": 120,
           "parameters": {"core": PIPELINE, "program": "selftest", "trace": trace, "max_cycles": LIMIT},
           "faults": list(faults)}
    return ExperimentRunner(tmp_path / "runs").run(parse_experiment(doc, base_dir=tmp_path, source="test"))


@pytest.mark.skipif(not HAS_ICARUS, reason="Icarus Verilog is not installed")
@pytest.mark.parametrize("trace", ["rvfi", "commit-log", "vcd-signals"])
def test_clean_pipeline_passes_through_every_trace_source(tmp_path: Path, trace: str):
    result = _run(tmp_path, trace)
    assert result.verdict is Verdict.PASS
    skipped = {r.contract.id for r in result.results if r.verdict is Verdict.SKIPPED}
    untimed = {"rv32i.lockstep_operands", "rv32i.forwarded_operands", "rv32i.cpi_budget", "rv32i.redirect_followed"}
    assert skipped == (untimed if trace == "commit-log" else set())


@pytest.mark.skipif(not HAS_ICARUS, reason="Icarus Verilog is not installed")
@pytest.mark.parametrize("trace", ["rvfi", "commit-log", "vcd-signals"])
@pytest.mark.parametrize("mutant", sorted(load_profile(PIPELINE).mutants))
def test_pipeline_mutants_first_diverge_where_the_model_predicts(tmp_path: Path, trace: str, mutant: str):
    result = _run(tmp_path, trace, [mutant])
    assert result.verdict is Verdict.FAIL
    events = read_events_jsonl(result.bundle_dir / "normalized_events.jsonl")
    first = next(
        e.attributes["rv.order"] for e in events
        if e.event_type == "cpu.retire" and any(
            f"rv.{f}" in e.attributes and e.attributes[f"rv.{f}"] != e.attributes[f"rv.ref_{f}"] for f in FIELDS)
    )
    source = sources()[trace].for_profile(load_profile(PIPELINE).signals)
    observed = [f for f in FIELDS if source.operands or f not in ("rs1_val", "rs2_val")]
    image = assemble(SELFTEST).image()
    fault = load_profile(PIPELINE).mutants[mutant].model_fault
    clean, faulty = Model(image).run(LIMIT).retires, Model(image, faults={fault}).run(LIMIT).retires
    if source.next_pc_inferred:
        clean, faulty = with_inferred_next_pc(clean), with_inferred_next_pc(faulty)
    assert first == first_divergence(clean, faulty, observed)


@pytest.mark.skipif(not HAS_ICARUS, reason="Icarus Verilog is not installed")
def test_wait_states_stretch_the_pipeline_clock_without_changing_results(tmp_path: Path):
    result = _run(tmp_path, "rvfi", [{"name": "mem_wait_states", "params": {"cycles": 3}}])
    failing = {r.contract.id for r in result.results if r.verdict is Verdict.FAIL}
    assert failing == {"rv32i.cpi_budget"}
