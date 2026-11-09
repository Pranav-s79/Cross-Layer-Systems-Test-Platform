# Contributing

## Setup

```sh
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

Python 3.11 or newer. The runtime depends only on PyYAML; do not add more
runtime dependencies. Dev-only tools go in the `dev` extra.

## Run the checks

CI runs these four commands plus `ruff check .` (pyflakes rules only) and `mypy` (scoped to `core/telemetry` and `core/units.py`). Run them before you push:

```sh
ruff check .
mypy
python -m pytest -q
crosslayer lint experiments/reference/suite.yaml --strict
crosslayer test-contracts crosslayer/integrations/sensor_pipeline/contract_tests.yaml --require-coverage
crosslayer suite experiments/reference/suite.yaml --out runs
```

The suite writes run bundles under `runs/`, which is gitignored.

## Regenerate schemas

The JSON Schemas in `schemas/` are generated. After changing a contract,
experiment, or contract-test format, run:

```sh
crosslayer schema --write schemas
```

A drift test fails if the committed files are out of date.

## Add an adapter

Generate a project that passes its own tests before any edits:

```sh
crosslayer new adapter my_system --dir integrations/
```

Then follow the generated README and [Writing an adapter](docs/writing-an-adapter.md).
See [Contract tooling](docs/tooling.md#scaffolding-an-adapter) for what the
scaffold contains.

## Boundaries

`crosslayer/core` must never import `crosslayer/integrations`. A test enforces
this.

## Commits

* One focused change per commit, with its tests.
* Subject in the imperative mood, at most 72 characters, for example
  "Add run --dry-run to validate and print the plan without running".
* Add a body when the reason is not obvious from the diff.
* Add a line under `Unreleased` in [CHANGELOG.md](CHANGELOG.md).
* Update the relevant page in `docs/` when behavior changes, concisely: short
  sentences, tables over prose, edit existing sections rather than repeat them.
  New kinds of tests go in [docs/testing.md](docs/testing.md); new extension
  points in [docs/extending.md](docs/extending.md).
* Pick small items from [docs/backlog.md](docs/backlog.md) and tick them in the
  same commit.
