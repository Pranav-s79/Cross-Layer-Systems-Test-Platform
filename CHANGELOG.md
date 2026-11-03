# Changelog

All notable changes are recorded here, in the
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format.

## [Unreleased]

- Add this changelog.

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
