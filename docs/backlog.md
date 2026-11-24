# Backlog

Small, self-contained improvements between roadmap phases. Each item should
fit in one commit with its tests. Larger work lives in [ROADMAP.md](../ROADMAP.md).

Work top to bottom and tick an item in the same commit that completes it. If
an item turns out to be already done or no longer sensible, strike it through
with a one-line reason and take the next one.

**Rules for every item**

* `python -m pytest` and the three reference commands from CI must pass:
  `crosslayer lint experiments/reference/suite.yaml --strict`,
  `crosslayer test-contracts crosslayer/integrations/sensor_pipeline/contract_tests.yaml --require-coverage`,
  `crosslayer suite experiments/reference/suite.yaml --out runs`.
* Regenerate schemas after any schema change: `crosslayer schema --write schemas`.
* No new runtime dependencies (`[project] dependencies`). Dev-only tools only
  where an item says so.
* `crosslayer/core` never imports `crosslayer/integrations`.
* Update the relevant page in `docs/` when behavior changes. Keep docs concise
  (short sentences, tables, no repetition) and the README short. Update
  `docs/testing.md` for a new kind of test and `docs/extending.md` for a new
  extension point.
* Numbers in docs come from runs made for that change, with the machine stated.

## Items

- [x] **CHANGELOG.md.** Keep a Changelog format. An `Unreleased` section plus
  a `0.1.0` section summarizing Phases 1 and 2 from the git history (nothing
  that is not in the history). From then on, each change adds a line under
  `Unreleased`.
- [x] **`crosslayer --version`.** Print the installed version via
  `importlib.metadata`. Test in `tests/test_cli.py`.
- [x] **CI: Python 3.12 and 3.14, pip cache.** Extend the test matrix in
  `.github/workflows/ci.yml` to 3.11–3.14 on both operating systems and enable
  `cache: pip` in `actions/setup-python`.
- [x] **`crosslayer run --dry-run`.** Call `ExperimentRunner.plan()` only and
  print what would run (adapter, mode, contracts, faults). Exit 0, or 4 on a
  config error. Test that no bundle directory is created.
- [x] **CONTRIBUTING.md.** Dev setup, running tests and the reference suite,
  regenerating schemas, adding an adapter (point to the scaffold), commit
  style. One line in the README docs table.
- [x] **`transitions`: optional `final` states.** At the end of the trace each
  group's last state must be in `final`; otherwise a violation at the group's
  last event. Loader, JSON Schema, lint (a `final` state unreachable from
  `initial` is an error), `docs/contract-language.md` (recommend pairing with
  `evidence` so a truncated log is INCONCLUSIVE), PASS and FAIL tests.
- [x] **Every subcommand's `--help` renders.** Parametrized test over the
  parser's subcommands; each exits 0 and mentions its own name.
- [x] **Cross-platform replay.** `replay.sh` needs a POSIX shell. Add
  `crosslayer replay BUNDLE [--out DIR]`: re-run the bundled
  `configuration.yaml`, then report whether the config hash and the functional
  fingerprint match the original. Mention it in `report.md` and
  `docs/run-bundle.md`. Test on a sensor-pipeline bundle.
- [x] **Ruff (pyflakes rules only).** Add `ruff` to the dev extras, select
  `F` and `E9` in `pyproject.toml`, fix what it reports, add a CI step. No
  formatting or import-order changes.
- [x] **`crosslayer adapters --check`.** Run `check_adapter_conformance` on each
  registered adapter, print problems, exit 1 if any.
- [x] **Per-stage timings.** Record monotonic durations of each runner stage
  (plan, build, execute, collect, normalize, evaluate, write) in
  `timings.json`, listed in the manifest and shown in `report.md`. Excluded
  from the functional fingerprint. Test that all stages are present and
  non-negative.
- [x] **`response`: optional `exclusive: true`.** Each response satisfies at
  most one trigger (first-in, first-out per correlation key). Default keeps
  current behavior. Update the "known limits" line in ROADMAP Phase 1, the
  schema, docs, and add tests (two triggers, one response).
- [x] **`--json` for `lint` and `test-contracts`.** Same style as
  `compare --json`. Tests parse the output.
- [x] **JUnit properties.** Add severity, boundary (`source -> destination`),
  and contract version as `<properties>` on each test case. Test by parsing
  the XML.
- [x] **Randomized tests for the expression validator.** Seeded `random`
  (no new dependency) builds small expressions, including disallowed syntax.
  Each must be rejected with `ExpressionError` at load time or evaluate
  without raising anything other than the documented errors.
- [x] **Reference fault `reorder`, part 1.** The consumer occasionally
  processes two queued packets out of order. Add the fault to `sim.py` and the
  adapter's `FaultSpec`s. With the fault off, output must stay byte-identical
  (the recorded fixtures still match); update the `sim.py` hash in
  `fixtures/README.md`.
- [x] **Reference fault `reorder`, part 2.** Find which contracts catch it. If
  none do, add one (for example processed sequence order) with contract tests.
  Add `experiments/reference/fault_reorder.yaml`, a suite entry, and extend
  `test_each_fault_trips_its_contracts`.
- [x] **Type-check `core/telemetry` and `core/units.py` with mypy.** Add mypy
  to the dev extras, config in `pyproject.toml`, fix findings, CI step for
  these paths only.
- [x] **Tutorial: your first contract.** `docs/tutorial.md`: add a contract to
  the reference pipeline, write its contract tests, lint, run, and read the
  report. Run every command in it before committing.
- [x] **`crosslayer show BUNDLE`.** Summarize an existing bundle from
  `contracts.json` and `manifest.json` (verdict, per-contract table, first
  violations) without re-running anything.
- [x] **Extend mypy to `core/contracts`.** Fix findings; keep behavior
  unchanged.
- [x] **Scaling benchmark.** `benchmarks/bench_scaling.py`: evaluate each rule
  kind at n and 4n events and print the time ratio, to catch a return of
  quadratic paths. A benchmark, not a test (timings are noisy on CI).
- [x] **Using CrossLayer in another repository's CI.** A GitHub Actions example
  in `docs/tooling.md` that runs a suite and publishes `junit.xml`.
- [x] **Extend mypy to `core/artifacts`, `core/adapters` and `core/orchestrator`.**
  Running mypy on them reports 4 errors (for example `junit.py:81` assigns an
  `int` to a `str` variable, `suite.py:294` calls `.upper()` on an optional
  verdict). Fix them and add the paths to `[tool.mypy] files`.
- [x] **`--json` for `show` and `verify`.** Every other reporting command has a
  machine-readable mode; these two print text only. Same style as `compare --json`.
  Tests parse the output.
- [x] **Test `crosslayer/cli.py` exit codes in one table.** Parametrized test
  that each subcommand returns the documented code for a missing file and a
  malformed config, so exit-code drift is caught in one place.
- [x] **Suite fail-fast option.** `crosslayer suite --fail-fast` stops after the
  first experiment whose verdict differs from its expectation and marks the
  rest as skipped in the suite summary. Test and document in `docs/tooling.md`.
- [x] **`crosslayer verify` on a tampered bundle.** Add tests that changing,
  deleting, or adding a file in a bundle makes `verify` exit non-zero and name
  the file; document the exit codes in `docs/run-bundle.md`.
- [x] **`--json` for `replay` and `diff-contracts`.** Every other reporting
  command now has a machine-readable mode; these two print text only.
- [x] **Test `crosslayer new`.** The test suite has no direct test of the CLI
  scaffold command; add one that scaffolds into a temp dir and runs
  `adapters --check`-style conformance on the result.
- [x] **Coverage in CI.** Add `pytest-cov` to the dev extras and report
  coverage for `crosslayer/core` in one CI job, so untested modules are
  visible. Report only; no threshold.
- [x] **Extend mypy to `crosslayer/cli.py`.** It is the largest module outside
  the checked paths; fix findings without changing behavior.
- [x] **Loader error-message tests.** `core/contracts/loader.py` raises many
  `ConfigError`s with locations; add a parametrized test that each names the
  offending key path so messages stay actionable.
- [x] **Extend mypy to the remaining `core` modules.** `core/schemas.py`,
  `core/errors.py` and `core/verdict.py` are outside `[tool.mypy] files`;
  `mypy crosslayer/core` reports no errors today. Add them so they stay clean.
- [x] **Test `core/artifacts/environment.py` directly.** No test file
  references it; cover the captured fields and their stability.
- [x] **Test the adapter registry directly.** No test references
  `core/adapters/registry.py`; cover lookup of an unknown adapter name and
  duplicate registration.
- [x] **Type-check the sensor-pipeline integration.** `mypy` reports two errors
  in `sim.py` (`crc8` called with a `bytearray`, `sys.stdout.reconfigure`).
  Fix them without changing simulator output; update the `sim.py` hash in
  `fixtures/README.md` and add the package to `[tool.mypy] files`.
- [x] **Direct tests for `core/contracts/selectors.py`.** Only two test files
  mention it; add parametrized cases for each selector form and its error
  messages.
- [x] **Direct tests for `core/adapters/conformance.py`.** Coverage is 84%
  (`pytest --cov=crosslayer`, Icarus not installed); the failure branches of
  `check_adapter_conformance` (lines 40-125) have no test that feeds it a broken adapter.
- [ ] **`crosslayer run --set key=value`.** Override experiment `parameters`
  from the command line (repeatable; values parsed as YAML scalars, dotted keys
  for nesting). The override is recorded in the bundle's `configuration.yaml`
  and config hash, so `replay` reproduces it. Unknown keys still fail plan.
- [ ] **`crosslayer suite --only ID[,ID...]`.** Run a subset of a suite plus
  the experiments they depend on (transitively), reporting the rest as
  `skipped`; sweep ids accept the base id (`random_programs`) or one seed
  (`random_programs@3`).
- [ ] **Compressed log inputs for textlog.** Accept `.gz` inputs (stdlib
  `gzip`): decompress into `raw_artifacts/` under the uncompressed name so
  evidence line references stay valid; record both SHA-256 values.
- [ ] **Multi-line log records for textlog.** An optional profile key
  `continuation: <regex>` appends matching lines to the previous record
  (stack traces, wrapped messages); the event keeps the first line's number
  and records how many lines it spans. Document it in `log-analysis.md`.
- [ ] **`crosslayer export BUNDLE`.** Write one `.zip` with the bundle and a
  short README (verdict, top hypotheses, how to verify and replay);
  `crosslayer verify` accepts the zip directly.

Coverage-only items below are lower priority: take them only when no
capability item above is open, and prefer folding such tests into a feature.

- [ ] **Test `core/orchestrator/experiment.py` config errors.** Coverage is 87%;
  add parametrized cases for the uncovered `ConfigError` paths, asserting each
  message names the offending key.
- [ ] **Test `core/orchestrator/process.py` failure paths.** Coverage is 90%;
  cover a command that exits non-zero, times out, or is missing.
- [ ] **Test `python -m crosslayer`.** `crosslayer/__main__.py` is at 0%
  coverage; add a subprocess test that `python -m crosslayer --version` exits 0.
- [ ] **Test `rv32i/simcache.py` misses.** Coverage is 87%; cover a corrupt or
  unreadable cache entry and `CROSSLAYER_CACHE_DIR=off`, without needing Icarus.

When the list runs out, review the code for bugs, missing tests, unclear
error messages, or documentation gaps, and add five new items here with a
one-line reason each.
