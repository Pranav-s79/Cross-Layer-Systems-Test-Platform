"""Core profiles: loading, validation, mutant application, and user-supplied cores."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from crosslayer.core.errors import ConfigError
from crosslayer.core.orchestrator import ExperimentRunner
from crosslayer.core.orchestrator.experiment import parse_experiment
from crosslayer.core.verdict import Verdict
from crosslayer.integrations.rv32i.adapter import Rv32iAdapter
from crosslayer.integrations.rv32i.iss import MODEL_FAULTS
from crosslayer.integrations.rv32i.profile import (
    CORES_DIR,
    apply_mutants,
    builtin_cores,
    load_profile,
)

HAS_ICARUS = shutil.which("iverilog") is not None and shutil.which("vvp") is not None
XOR_LINE = "\t\t\tinstr_xori || instr_xor:\n\t\t\t\talu_out = reg_op1 ^ reg_op2;"


def _copy_core(tmp_path: Path, **changes) -> Path:
    """Copy the PicoRV32 profile to `tmp_path/mycore` and apply top-level changes."""
    root = tmp_path / "mycore"
    shutil.copytree(CORES_DIR / "picorv32", root)
    doc = yaml.safe_load((root / "core.yaml").read_text(encoding="utf-8"))
    for key, value in changes.items():
        if value is None:
            doc.pop(key, None)
        else:
            doc[key] = value
    (root / "core.yaml").write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return root


def test_builtin_profiles_load_and_every_mutant_applies_exactly_once():
    assert "picorv32" in builtin_cores()
    for core in builtin_cores():
        profile = load_profile(core, model_faults=MODEL_FAULTS)
        assert profile.mutants, core
        for name in profile.mutants:
            texts, _ = apply_mutants(profile, [name])
            changed = [s for s in profile.sources if texts[s] != profile.file(s).read_text(encoding="utf-8")]
            assert changed or profile.mutants[name].defines, name
        assert len(profile.digest()) == 12


def test_picorv32_mutants_name_their_reference_model_fault():
    profile = load_profile("picorv32")
    assert {m.model_fault for m in profile.mutants.values()} == MODEL_FAULTS


@pytest.mark.parametrize(
    "changes, match",
    [
        ({"trace": "jtag"}, "unsupported value 'jtag'"),
        ({"isa": "rv64gc"}, "unsupported value 'rv64gc'"),
        ({"sources": None}, "sources: is required"),
        ({"sources": []}, "at least one entry"),
        ({"sources": ["missing.v"]}, "file not found"),
        ({"sources": ["../picorv32/picorv32.v"]}, "inside the profile's directory"),
        ({"testbench": "picorv32.v"}, "separate from the testbench"),
        ({"schema_version": 2}, "unsupported schema_version"),
        ({"name": "My Core"}, "lowercase"),
        ({"surprise": 1}, "unknown keys"),
        ({"defines": ["BAD DEFINE"]}, "invalid define"),
        ({"clock_period_ns": 0}, "positive integer"),
        ({"signals": None}, "signals: is required when trace lists vcd-signals"),
        ({"signals": {"clock": "tb.clk", "valid": "tb.v", "fields": {"pc": "tb.pc"}}}, "must map 'insn'"),
        ({"signals": {"clock": "tb.clk", "valid": "tb.v", "fields": {"pc": "a", "insn": "b", "cpi": "c"}}},
         r"unknown fields \['cpi'\]"),
        ({"signals": {"clock": "tb clk", "valid": "tb.v", "fields": {"pc": "a", "insn": "b"}}}, "hierarchical"),
        ({"trace": ["rvfi", "rvfi"]}, "distinct formats"),
        ({"mutants": {"m": {"target_subsystem": "alu", "defines": ["X"], "target_interface": "nowhere"}}},
         "unknown interface"),
        ({"mutants": {"m": {"target_subsystem": "gpu", "defines": ["X"]}}}, "unknown subsystem"),
        ({"mutants": {"m": {"target_subsystem": "alu", "defines": ["X"], "model_fault": "nope"}}},
         "unknown reference-model fault"),
        ({"mutants": {"m": {"target_subsystem": "alu"}}}, "at least one edit or define"),
        ({"mutants": {"m": {"target_subsystem": "alu", "edits": [{"file": "tb.v", "find": "a", "replace": "b"}]}}},
         "one of the core's sources"),
        ({"mutants": {"m": {"target_subsystem": "alu", "edits": [{"file": "picorv32.v", "find": "a", "replace": "a"}]}}},
         "differs from find"),
    ],
)
def test_invalid_profiles_name_the_offending_field(tmp_path: Path, changes, match):
    root = _copy_core(tmp_path, **changes)
    with pytest.raises(ConfigError, match=match):
        Rv32iAdapter().faults_for({"core": str(root)}, tmp_path)


def test_unknown_core_lists_the_builtin_cores(tmp_path: Path):
    with pytest.raises(ConfigError, match=r"unknown core 'nope' \(built-in: \['picorv32'"):
        Rv32iAdapter().faults_for({"core": "nope"}, tmp_path)


def test_stale_mutant_fails_loudly_instead_of_doing_nothing(tmp_path: Path):
    root = _copy_core(tmp_path, mutants={"stale": {
        "target_subsystem": "alu", "edits": [{"file": "picorv32.v", "find": "no such text", "replace": "x"}]}})
    profile = load_profile(str(root))
    with pytest.raises(ConfigError, match="expected 1 match, found 0"):
        apply_mutants(profile, ["stale"])


def test_fault_catalog_follows_the_selected_core(tmp_path: Path):
    root = _copy_core(tmp_path, name="mycore", mutants={"xor_as_or": {
        "description": "XOR computes OR.", "target_subsystem": "alu", "target_interface": "writeback",
        "edits": [{"file": "picorv32.v", "find": XOR_LINE, "replace": XOR_LINE.replace("^", "|")}]}})
    adapter = Rv32iAdapter()
    default = {f.name for f in adapter.faults_for({}, tmp_path)}
    custom = {f.name for f in adapter.faults_for({"core": "mycore"}, tmp_path)}
    by_path = {f.name for f in adapter.faults_for({"core": str(root / "core.yaml")}, Path("/"))}
    assert default == MODEL_FAULTS | {"mem_wait_states"}
    assert custom == by_path == {"xor_as_or", "mem_wait_states"}
    assert {f.name for f in adapter.faults()} == default


def _experiment(tmp_path: Path, **doc):
    base = {"schema_version": 1, "name": "custom-core", "adapter": "rv32i", "seed": 1, "timeout_s": 120,
            "parameters": {"core": "mycore", "program": "selftest"}}
    base.update(doc)
    return parse_experiment(base, base_dir=tmp_path, source="test")


def test_builtin_mutant_is_rejected_for_a_core_that_does_not_define_it(tmp_path: Path):
    _copy_core(tmp_path, mutants={})
    with pytest.raises(ConfigError, match="unknown faults"):
        ExperimentRunner(tmp_path / "runs").plan(_experiment(tmp_path, faults=["sub_as_add"]))


@pytest.mark.skipif(not HAS_ICARUS, reason="Icarus Verilog is not installed")
def test_user_core_and_mutant_run_end_to_end_without_python_changes(tmp_path: Path):
    _copy_core(tmp_path, name="mycore", mutants={"xor_as_or": {
        "description": "XOR computes OR.", "target_subsystem": "alu", "target_interface": "writeback",
        "edits": [{"file": "picorv32.v", "find": XOR_LINE, "replace": XOR_LINE.replace("^", "|")}]}})
    runner = ExperimentRunner(tmp_path / "runs")

    clean = runner.run(_experiment(tmp_path))
    assert clean.verdict is Verdict.PASS
    assert clean.manifest["build"]["core"]["name"] == "mycore"

    mutant = runner.run(_experiment(tmp_path, faults=["xor_as_or"]))
    assert mutant.verdict is Verdict.FAIL
    failed = {r.contract.id for r in mutant.results if r.verdict is Verdict.FAIL}
    assert "rv32i.lockstep_writeback" in failed
    diff = (mutant.bundle_dir / "raw_artifacts" / "rtl.diff").read_text(encoding="utf-8")
    assert "# core: mycore" in diff and "reg_op1 | reg_op2" in diff
