# Changelog

All notable changes are recorded here, in the
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format.

## [Unreleased]

- Add `crosslayer suite --only ID[,ID...]` to run a subset of a suite plus its dependencies (a sweep id selects every seed, `id@N` one seed); the rest are reported as skipped with reason `not selected`.
- Add `crosslayer run --set KEY=VALUE` to override experiment parameters from the command line; the override is recorded in the bundle's configuration and config hash, so `replay` reproduces it.
- Add `crosslayer dashboard`: writes one self-contained HTML page from the run index (suite health per day, failing contracts by owner, regressions, flaky contracts, localization accuracy), with no scripts or network resources.
- Add `crosslayer flaky`: lists contracts (and run verdicts) whose verdict differs between runs with the same experiment, seed, and config hash, with verdict counts, flips, and the first differing run; `--fail-on-flaky` exits 1.
- Add `crosslayer regressions`: compares each experiment and seed's latest run with its earlier runs (verdict flips, newly failing contracts, stage timing and first-violation shifts beyond a tolerance), links each finding to the first run where it appeared, and exits 1 on a regression.
- Add a SQLite run index (Phase 11): `crosslayer index add PATH...` records runs, contract results with first-violation time, fault labels, ground truth, timings, and top hypotheses; `crosslayer index runs` lists them and `crosslayer history CONTRACT_ID` shows one contract's verdicts over time.
- Add guided follow-up experiments (Phase 8): `SystemAdapter.diagnostic_actions` declares experiment variants with a `clears` or `observes` outcome model, and `crosslayer diagnose` runs them by expected information gain, updating the suspects by Bayes' rule, and writes the trail to `<bundle>.diagnosis/`. Over 76 failing labeled runs, top-1 goes from 69 to 73 with 46 follow-up runs (65 to run every action).
- Add reference components to the sensor-pipeline simulator (`reference_components`, `sim.py --reference`) and the actions that swap each one in.
- Add `nop_spacing` to the RV32I adapter (`asm.assemble(spacing=N)`) and the `space_instructions` and `observe_operands` actions.
- Add a third RV32I core, a five-stage pipeline with forwarding, a load-use stall, and branch flushes (learn-fpga `pipeline6.v`, BSD-3-Clause, unmodified), verified through a testbench pipeline monitor in all three trace formats, with `experiments/rv32i-pipeline/`.
- Add four hazard mutants (`no_mem_forwarding`, `no_wb_forwarding`, `no_load_use_stall`, `no_branch_flush`), each predicted by an independent pipeline model in `iss.py` (`PIPELINE_FAULTS`) and caught at the predicted instruction in every trace format and on every seed of a hazard sweep.
- Add hazard contracts for pipelined cores: `rv32i.forwarded_operands` (operands within the bypass window) and `rv32i.redirect_followed` (no wrong-path retirement); SKIPPED on cores that are not pipelined.
- Make RV32I contracts local: results, next PCs, and data accesses are checked against what the operands actually read call for (`rv.exp_*`), operands only for the same instruction, loaded values in the new `rv32i.load_data`, and the end-to-end path in the new `rv32i.reference_path`. Localization top-1 rose from 19/26 to 29/36 fault experiments (40/40 sweep seeds); top-3 stays at 100%.
- Add `SystemAdapter.describe_for(parameters, base_dir)`: localization and ground truth use the structure of the run's configuration; contracts SKIPPED by their precondition add no graph edge.
- Add hazard coverage bins (read after write at distance 1-3, load-use, branch and store dependences) and `random_mix: hazards` for random programs; the default mix is unchanged.
- Add core-profile keys `includes`, `top`, `memory_images`, and `pipeline`, and the `next_pc_inferred` trace-source flag.
- Add direct tests for adapter conformance checks: every failure branch of `check_adapter_conformance` and `check_events_against_catalog`.
- Add five backlog items (adapter conformance, experiment config errors, process failures, `python -m crosslayer`, simcache misses) found from coverage gaps.
- Add a "Locate the fault" step to `report.html` (ranked hypotheses with evidence and counter-evidence linked to contract rows) and the top three hypotheses to `crosslayer show`; older bundles render as before.
- Add the first failing event to `crosslayer show`: its raw line and observed vs reference values side by side, paired by `ref_` / `_ref` attribute names (`first_failure` in `--json`).
- Add assembler and decoder round-trip tests: seeded random RV32I instructions decode back to their operands, and reserved encodings decode as illegal.
- Add a JSON Schema for RV32I core profiles (`crosslayer schema rv32i-core-profile`); the built-in `core.yaml` files reference it for editor validation.
- Add direct tests for event selectors (each filter form, matching rules, error locations).
- Type-check the sensor-pipeline integration with mypy (two `sim.py` fixes, simulator output unchanged).
- Add fault localization: every bundle ranks subsystems (with the likely interface) from failing contracts, earliest first, with counter-evidence from passing contracts and an ambiguous/confident status with its reason (`localization.json`, `report.md`). Top-1 19/26 and top-3 26/26 over all labeled fault experiments.
- Add `ground_truth` to experiments whose fault is in their inputs, and `crosslayer localize` (one bundle, or `--score` with `--min-top1` / `--min-top3` gates).
- Describe RV32I's instruction, decode, store-data, and load-data paths in its system graph.
- Add `crosslayer suite -j N` (`-j auto`): experiments run in worker processes as soon as their dependencies finish, with reports in sequential order. The RV32I suite went from 18.4 s to 5.4 s with `-j 8` (Windows, 8 cores); CI runs suites with `-j auto`.
- Claim run bundle directories atomically, so parallel runs of one configuration never collide.
- Guard `python -m crosslayer` so worker processes can import it without running the CLI.
- Cut per-run overhead: YAML parsed with libyaml when available, core and log profiles cached until they change on disk, one adapter instance and environment record per runner, and bundle files hashed as they are written. Measured on Windows (8 cores, Python 3.11): the RV32I baseline went from 3.45 s to 0.77 s per run and a 5G log run from about 1.1 s to 0.17 s.
- Cache compiled RV32I simulations by a hash of their exact inputs (`CROSSLAYER_CACHE_DIR`, `off` to disable).
- Add a two-UE 5G fixture with a 40 ms gNB clock skew, and a variant where only one UE's registration is missing.
- Add upstream-verified log profiles and clearly labeled synthetic fixtures for Open5GS v2.8.0 AMF/SMF and OCUDU release 26.10 gNB logs.
- Add composite text-log correlation joins with exact id-pair matching, bounded unambiguous time fallback, and per-event confidence metadata.
- Add a synthetic Open5GS/OCUDU fixture suite with registration, PDU-session, and RRC-state contracts; dropped, delayed, and reordered variants each fail only their target contract.
- Add the `textlog` adapter: YAML log profiles turn line-oriented text logs into events, with one profile per input, per-source UTC offset, year and clock offset, and a synthetic two-source example in `experiments/textlog/`.
- Add seed sweeps to suites (`seeds: [..]` or `{count, start}`), with per-sweep counts and coverage bins summed across seeds.
- Add the `coverage` rule kind and a coverage section in `report.md` for adapters that report `stats.coverage` bins.
- Report RV32I ISA coverage (mnemonics, branch directions, access width and offset); add `coverage_contracts.yaml`, `isa_coverage`, and 20-seed random-program sweeps for both cores.
- Generate random RV32I programs with branches, jumps, and every load/store width and offset; fix their output register (it wrote the port address).
- Treat unknown RVFI write data in unwritten byte lanes as don't-care instead of dropping the retirement.
- `rv32i.cpi_budget` (version 3) excludes shifts, whose latency depends on the shift amount.
- Add a second RV32I core, FemtoRV32 Quark (BSD-3-Clause, no RVFI), verified through a testbench retirement monitor and its waveform, with six ported mutants and `experiments/rv32i-quark/`.
- Add the `vcd-signals` RV32I trace source: retirements sampled from a streamed waveform through a profile signal map, so a core needs no trace printing.
- Add the `commit-log` RV32I trace source (Spike `--log-commits` format), `parameters.trace`, and a `+commit_log` testbench mode; `lockstep_operands` and `cpi_budget` (version 2) are SKIPPED on traces without operand values or cycles.
- Move RV32I trace parsing behind a `TraceSource` interface (`rv32i/traces/`) that yields typed records.
- Add `docs/milestones.md`, the step-by-step queue for Phases 3 and 4.
- Add RV32I core profiles (`cores/<name>/core.yaml`): RTL sources, testbench, defines,
  provenance and mutants in YAML, selected with `parameters.core`; mutants move out of Python.
- Add `SystemAdapter.faults_for(parameters, base_dir)` so an adapter's fault catalog can depend on its configuration.
- Add direct tests for adapter discovery in `core/adapters/registry.py`.
- Add direct tests for `core/artifacts/environment.py`.
- Extend mypy checks to all of `crosslayer/core`.
- Add five new items to the backlog.
- Add a parametrized test that contract loader errors name the offending key path.
- Extend mypy checks to `crosslayer/cli.py`.
- Add `pytest-cov` to the dev extras and a CI job that reports coverage for `crosslayer/core`.
- Test that `crosslayer new adapter` writes an adapter that passes conformance.
- Add `--json` to `crosslayer replay` and `crosslayer diff-contracts`.
- Test that `crosslayer verify` names modified, missing and added bundle files, and document its exit codes.
- Add `crosslayer suite --fail-fast` to stop after the first unmet experiment and mark the rest skipped.
- Test that every subcommand exits 4 on a missing or malformed input file or bundle.
- Add `--json` to `crosslayer show` and `crosslayer verify`.
- Extend mypy checks to `core/artifacts`, `core/adapters` and `core/orchestrator`.
- Document a GitHub Actions job for running a suite in another repository's CI.
- Add `benchmarks/bench_scaling.py` to check that each rule kind scales linearly.
- Extend mypy checks to `core/contracts`.
- Add `crosslayer show` to summarize an existing run bundle without re-running it.
- Add a tutorial for writing, testing and running a first contract.
- Add mypy (strict untyped-def checks) for `core/telemetry` and `core/units.py`, with a CI step.
- Add the Phase 3 RV32I adapter for unmodified PicoRV32 under Icarus Verilog,
  with an assembler, reference ISA model, RVFI normalization, 10 contracts,
  seven injected faults, seeded random programs, and a simulator suite.
- Fix Icarus compile paths when a suite uses a relative output directory.
- Add a self-contained `report.html` to every run bundle with five-step
  navigation, contract search and filters, expandable evidence, and replay guidance.
- Add a `reorder` fault to the reference sensor pipeline simulator.
- Add the `pipeline.processing_order` contract, the `fault_reorder` experiment and its suite entry.

- Add seeded randomized tests for the expression validator.
- Add severity, boundary and contract version as properties on each JUnit test case.
- Add `--json` to `crosslayer lint` and `crosslayer test-contracts`.
- Add `exclusive: true` to the `response` rule so each response satisfies at most one trigger.
- Record per-stage timings in `timings.json` and show them in `report.md`.
- Add `crosslayer adapters --check` to run conformance checks on registered adapters.
- Add ruff (pyflakes rules only) to the dev extras and CI.
- Add `crosslayer replay` to re-run a bundle on any OS and compare config hash and fingerprint.
- Add docs on how CrossLayer is tested and how to extend it.
- Test that every subcommand's `--help` renders.
- Add optional `final` states to the `transitions` rule, with lint check CL008.
- Add CONTRIBUTING.md.
- Add this changelog.
- Add `crosslayer --version`.
- Add `crosslayer run --dry-run` to validate and print the plan without running.
- Test on Python 3.11 to 3.14 in CI and cache pip downloads.
- Tighten the README, roadmap, and docs; fix outdated notes on suites and `verify`.

## [0.1.0]

### Phase 1: verification core

- Verification core with a contract engine and reproducible run bundles.
- Fixes for defects found in the Phase 1 review.

### Phase 2: contract tooling and developer experience

- Contract tests: known-good and known-bad traces per contract.
- Contract linter with adapter event catalogs.
- JSON Schemas for contracts, experiments, and contract tests.
- Experiment suites with dependency ordering and expected verdicts.
- Contract versioning, per-owner changelogs, and change-aware comparison.
- `crosslayer new adapter` scaffolding.
- Every suite experiment is validated before any runs.
