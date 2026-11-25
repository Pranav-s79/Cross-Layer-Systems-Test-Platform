"""RV32I guided follow-ups: NOP spacing, the actions each configuration offers,
and diagnoses that fix localization misses."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from crosslayer.core.diagnosis import diagnose
from crosslayer.core.errors import ConfigError
from crosslayer.core.orchestrator import ExperimentRunner, load_experiment, parse_experiment
from crosslayer.integrations.rv32i.adapter import MAX_NOP_SPACING, Rv32iAdapter
from crosslayer.integrations.rv32i.asm import NOP, assemble
from crosslayer.integrations.rv32i.iss import Model
from crosslayer.integrations.rv32i.randprog import generate

ROOT = Path(__file__).resolve().parents[2]
HAS_ICARUS = shutil.which("iverilog") is not None and shutil.which("vvp") is not None


def test_spacing_pads_every_instruction_word_but_not_data():
    source = ".text\n    li x5, 0x12345\n    beq x0, x0, end\n    addi x6, x0, 1\nend:\n    ebreak\nd:  .word 7, 8\n"
    plain, spaced = assemble(source), assemble(source, spacing=2)
    instructions = [w for w in plain.words if w not in (7, 8)]
    assert len(spaced.words) == 3 * len(instructions) + 2  # data words are not padded
    assert spaced.words[-2:] == [7, 8]
    assert spaced.labels["end"] == 3 * plain.labels["end"]
    assert spaced.words[1:3] == [NOP, NOP] and spaced.words[4:6] == [NOP, NOP]  # LI's two words are each padded
    assert spaced.source_lines[3 * 4] == plain.source_lines[4]  # source lines follow the real words


def test_spaced_programs_terminate_and_keep_their_path_unless_an_address_steers_it():
    selftest = (ROOT / "crosslayer/integrations/rv32i/programs/selftest.s").read_text(encoding="utf-8")
    plain = Model(assemble(selftest).image()).run()
    spaced = Model(assemble(selftest, spacing=4).image()).run()
    assert [r.mnemonic for r in spaced.retires if r.insn != NOP] == [r.mnemonic for r in plain.retires]
    # A random program folds JAL link values (addresses) into data, which can
    # steer a later branch differently once code moves; it still terminates.
    for seed in range(1, 6):
        run = Model(assemble(generate(seed, mix="hazards"), spacing=4).image()).run()
        assert run.halt == "ebreak" and run.bus_errors == [] and len(run.outputs) == 1


def test_a_literal_jalr_offset_after_auipc_is_scaled():
    source = ".text\n    auipc x16, 0\n    jalr x5, 12(x16)\n    addi x6, x0, 1\n    addi x7, x0, 2\n    ebreak\n"
    run = Model(assemble(source, spacing=3).image()).run()
    assert [r.rd for r in run.retires if r.insn != NOP][:3] == [16, 5, 7]  # x6's ADDI is skipped, as unspaced
    with pytest.raises(ValueError, match="non-negative"):
        assemble(source, spacing=-1)


def test_nop_spacing_is_validated():
    adapter = Rv32iAdapter()
    for bad in (-1, MAX_NOP_SPACING + 1, True, "4"):
        with pytest.raises(ConfigError, match="nop_spacing"):
            adapter.validate_parameters({"nop_spacing": bad})


@pytest.mark.parametrize("parameters, expected", [
    ({"core": "picorv32"}, []),
    ({"core": "picorv32", "trace": "commit-log"}, ["observe_operands"]),
    ({"core": "learnfpga-pipeline"}, ["space_instructions"]),
    ({"core": "learnfpga-pipeline", "trace": "commit-log"}, ["space_instructions", "observe_operands"]),
    ({"core": "learnfpga-pipeline", "nop_spacing": 4}, []),
    ({"core": "femtorv32-quark", "trace": "commit-log"}, ["observe_operands"]),
])
def test_each_configuration_offers_the_actions_that_can_separate_its_suspects(tmp_path, parameters, expected):
    actions = Rv32iAdapter().diagnostic_actions(parameters, tmp_path)
    assert [a.name for a in actions] == expected
    for action in actions:
        if action.name == "space_instructions":
            assert action.parameters == {"nop_spacing": 4} and "rv32i.cpi_budget" in action.ignores
            assert action.clear_probability("hazard", "bypass") == 1.0
            assert action.clear_probability("hazard", "pipeline_control") == 0.0
        else:
            assert action.parameters["trace"] in ("rvfi", "vcd-signals") and action.observes


def _diagnosed(tmp_path: Path, path: Path):
    run = ExperimentRunner(tmp_path / "runs").run(load_experiment(path))
    return run, diagnose(run.bundle_dir, runner_for=lambda d: ExperimentRunner(d))


@pytest.mark.skipif(not HAS_ICARUS, reason="Icarus Verilog is not installed")
def test_spacing_out_the_program_shows_a_missing_load_use_stall_is_hazard_logic(tmp_path: Path):
    run, diag = _diagnosed(tmp_path, ROOT / "experiments/rv32i-pipeline/fault_no_load_use_stall.yaml")
    assert run.manifest["localization"]["top"][0]["subsystem"] == "lsu"  # observation latency (a Phase 7 miss)
    assert [(s.action, s.outcome) for s in diag.steps] == [("space_instructions", "pass")]
    assert diag.top == "hazard"


@pytest.mark.skipif(not HAS_ICARUS, reason="Icarus Verilog is not installed")
def test_tracing_operands_shows_a_commit_log_failure_is_the_register_file(tmp_path: Path):
    run, diag = _diagnosed(tmp_path, ROOT / "experiments/rv32i/commit_log_regfile_wrong_rd.yaml")
    assert run.manifest["localization"]["top"][0]["subsystem"] != "regfile"
    assert [s.action for s in diag.steps] == ["observe_operands"] and diag.steps[0].outcome == "regfile"
    assert diag.top == "regfile"


@pytest.mark.skipif(not HAS_ICARUS, reason="Icarus Verilog is not installed")
def test_a_timing_only_failure_gets_no_spacing_action(tmp_path: Path):
    doc = {"schema_version": 1, "name": "slow", "adapter": "rv32i", "seed": 1, "timeout_s": 120,
           "parameters": {"core": "learnfpga-pipeline", "program": "selftest", "max_cycles": 20_000},
           "faults": [{"name": "mem_wait_states", "params": {"cycles": 3}}]}
    run = ExperimentRunner(tmp_path / "runs").run(parse_experiment(doc, base_dir=tmp_path, source="t"))
    diag = diagnose(run.bundle_dir, runner_for=lambda d: ExperimentRunner(d))
    assert diag.status == "no_actions"  # NOPs would change the CPI profile, not test the fault
