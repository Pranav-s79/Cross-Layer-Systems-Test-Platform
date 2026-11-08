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
- [ ] **Reference fault `reorder`, part 2.** Find which contracts catch it. If
  none do, add one (for example processed sequence order) with contract tests.
  Add `experiments/reference/fault_reorder.yaml`, a suite entry, and extend
  `test_each_fault_trips_its_contracts`.
- [ ] **Type-check `core/telemetry` and `core/units.py` with mypy.** Add mypy
  to the dev extras, config in `pyproject.toml`, fix findings, CI step for
  these paths only.
- [ ] **Tutorial: your first contract.** `docs/tutorial.md`: add a contract to
  the reference pipeline, write its contract tests, lint, run, and read the
  report. Run every command in it before committing.
- [ ] **`crosslayer show BUNDLE`.** Summarize an existing bundle from
  `contracts.json` and `manifest.json` (verdict, per-contract table, first
  violations) without re-running anything.
- [ ] **Extend mypy to `core/contracts`.** Fix findings; keep behavior
  unchanged.
- [ ] **Scaling benchmark.** `benchmarks/bench_scaling.py`: evaluate each rule
  kind at n and 4n events and print the time ratio, to catch a return of
  quadratic paths. A benchmark, not a test (timings are noisy on CI).
- [ ] **Using CrossLayer in another repository's CI.** A GitHub Actions example
  in `docs/tooling.md` that runs a suite and publishes `junit.xml`.

When the list runs out, review the code for bugs, missing tests, unclear
error messages, or documentation gaps, and add five new items here with a
one-line reason each.
