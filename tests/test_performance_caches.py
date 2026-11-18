"""Caches added for speed must never serve stale results."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from crosslayer.core.artifacts.bundle import RunBundle, sha256_file
from crosslayer.core.orchestrator import ExperimentRunner
from crosslayer.core.yamlio import SAFE_LOADER, YAMLError, load_yaml
from crosslayer.integrations.rv32i import simcache
from crosslayer.integrations.rv32i.profile import CORES_DIR
from crosslayer.integrations.rv32i.profile import load_profile as load_core
from crosslayer.integrations.textlog.profile import load_profile as load_log_profile
from tests.core.test_runner import FakeAdapter, experiment

HAS_ICARUS = shutil.which("iverilog") is not None and shutil.which("vvp") is not None


def test_yaml_loader_is_safe_and_uses_libyaml_when_available():
    assert load_yaml("a: [1, 2]\nb: {c: d}\n") == {"a": [1, 2], "b": {"c": "d"}}
    if yaml.__with_libyaml__:
        assert SAFE_LOADER is yaml.CSafeLoader
    with pytest.raises(YAMLError):
        load_yaml("!!python/object/apply:os.system ['echo unsafe']")


def test_bundle_index_rehashes_files_changed_after_writing(tmp_path: Path):
    bundle = RunBundle.create(tmp_path, "run")
    path = bundle.write_text("report.md", "first\n")
    [entry] = bundle.file_index()
    assert entry["sha256"] == sha256_file(path)
    path.write_text("changed after writing, same process\n", encoding="utf-8")
    [entry] = bundle.file_index()
    assert entry["sha256"] == sha256_file(path) and entry["bytes"] == path.stat().st_size


def test_runner_reuses_one_adapter_and_environment_across_runs(tmp_path: Path):
    calls = []

    def loader(ref):
        calls.append(ref)
        return FakeAdapter()

    runner = ExperimentRunner(tmp_path / "runs", adapter_loader=loader)
    first = runner.run(experiment(tmp_path))
    second = runner.run(experiment(tmp_path, seed=9))
    assert calls == ["fake"]
    env = [(r.bundle_dir / "versions.json").read_text(encoding="utf-8") for r in (first, second)]
    assert env[0] == env[1]


def test_core_profile_cache_reloads_an_edited_profile(tmp_path: Path):
    root = tmp_path / "core"
    shutil.copytree(CORES_DIR / "picorv32", root)
    assert load_core(str(root)).clock_period_ns == 10
    text = (root / "core.yaml").read_text(encoding="utf-8")
    (root / "core.yaml").write_text(text.replace("clock_period_ns: 10", "clock_period_ns: 20  # edited"),
                                    encoding="utf-8")
    assert load_core(str(root)).clock_period_ns == 20
    digest = load_core(str(root)).digest()
    (root / "tb.v").write_text((root / "tb.v").read_text(encoding="utf-8") + "\n// edited\n", encoding="utf-8")
    assert load_core(str(root)).digest() != digest


def test_log_profile_cache_reloads_an_edited_profile(tmp_path: Path):
    source = Path(load_log_profile("synthetic_server").path)
    copy = tmp_path / "server.yaml"
    copy.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    before = load_log_profile(str(copy), tmp_path, subsystems={"client", "server"})
    assert load_log_profile(str(copy), tmp_path, subsystems={"client", "server"}) is before  # cached
    copy.write_text(copy.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8")
    after = load_log_profile(str(copy), tmp_path, subsystems={"client", "server"})
    assert after is not before and after == before  # reloaded; same content apart from a comment


def test_compile_key_tracks_every_input(tmp_path: Path):
    tool = tmp_path / "iverilog"
    tool.write_bytes(b"v1")
    base = simcache.compile_key([str(tool)], ["-g2005"], [("tb.v", "module tb; endmodule")])
    assert base == simcache.compile_key([str(tool)], ["-g2005"], [("tb.v", "module tb; endmodule")])
    assert base != simcache.compile_key([str(tool)], ["-g2005", "-DX"], [("tb.v", "module tb; endmodule")])
    assert base != simcache.compile_key([str(tool)], ["-g2005"], [("tb.v", "module tb; /* m */ endmodule")])
    tool.write_bytes(b"version 2")  # an upgraded tool changes size and mtime
    assert base != simcache.compile_key([str(tool)], ["-g2005"], [("tb.v", "module tb; endmodule")])


def test_cache_can_be_disabled_and_misses_are_harmless(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(simcache.ENV, "off")
    assert simcache.cache_dir() is None
    assert simcache.fetch("k", tmp_path / "sim.vvp") is False
    simcache.store("k", tmp_path / "missing.vvp")  # no cache, no error
    monkeypatch.setenv(simcache.ENV, str(tmp_path))
    assert simcache.fetch("absent", tmp_path / "sim.vvp") is False
    simcache.store("k", tmp_path / "missing.vvp")  # unreadable image: ignored


@pytest.mark.skipif(not HAS_ICARUS, reason="Icarus Verilog is not installed")
def test_second_build_reuses_the_compiled_simulation(tmp_path: Path, monkeypatch):
    from crosslayer.core.orchestrator.experiment import parse_experiment
    from crosslayer.core.verdict import Verdict

    monkeypatch.setenv(simcache.ENV, str(tmp_path / "cache"))
    doc = {"schema_version": 1, "name": "cache", "adapter": "rv32i", "seed": 1, "timeout_s": 120,
           "parameters": {"program": "selftest"}}
    runner = ExperimentRunner(tmp_path / "runs")
    first = runner.run(parse_experiment(doc, base_dir=tmp_path, source="t"))
    second = runner.run(parse_experiment({**doc, "seed": 2}, base_dir=tmp_path, source="t"))
    mutant = runner.run(parse_experiment({**doc, "faults": ["sub_as_add"]}, base_dir=tmp_path, source="t"))
    hits = [r.manifest["build"]["compile_cache"]["hit"] for r in (first, second, mutant)]
    assert hits == [False, True, False]
    assert first.verdict is second.verdict is Verdict.PASS and mutant.verdict is Verdict.FAIL
