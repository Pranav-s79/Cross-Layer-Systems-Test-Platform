# Changelog

All notable changes are recorded here, in the
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format.

## [Unreleased]

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
