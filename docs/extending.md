# Extending CrossLayer

Where to plug in, smallest change first. Every extension ships with tests
([testing.md](testing.md)) and a short docs update.

## Extension points

| To add | Do this | Core changes? |
|---|---|---|
| **A contract** | Add it to a contracts YAML file, plus a PASS and a FAIL case in the contract tests | No |
| **A fault** | Implement it in the system or adapter, declare a `FaultSpec` with its true target, add an experiment with `expect: fail` to the suite | No |
| **A new system** | `crosslayer new adapter NAME`, then follow [writing-an-adapter.md](writing-an-adapter.md) | No |
| **A lint check** | Add a code to `core/contracts/lint.py` (or `core/orchestrator/lint.py` for experiments) and to the table in [tooling.md](tooling.md) | Yes, small |
| **A rule kind** | Subclass `Rule` in `core/contracts/rules.py` with `@register_rule` (`parse`, `evaluate`, `selectors`, `requirements`), add it to `_RULES` in `core/schemas.py`, regenerate schemas, document it in [contract-language.md](contract-language.md) | Yes |
| **A CLI command** | Add a subparser in `crosslayer/cli.py` | Yes, small |

The first three need no core changes. That's the design goal: each new domain
plugs in from outside, and `test_import_boundary.py` keeps it that way.

## Rules for any extension

- Validate early: bad config fails at load or `plan()`, before anything runs.
- No vacuous passes: a rule with nothing to check is INCONCLUSIVE.
- Every finding links to evidence (events and raw log lines).
- No new runtime dependencies (the core uses only the standard library and PyYAML).

## Where it's going

Small, scoped items are in [backlog.md](backlog.md). The larger direction is
in the [roadmap](../ROADMAP.md):

| Next | What it adds | Built from |
|---|---|---|
| Phase 3 · RTL | Verify a processor from its simulation traces (PC sequencing, lockstep with a reference model) | A new adapter + `successive` contracts |
| Phase 4 · 5G logs | Join RAN and core logs by UE ids; registration and session contracts | A new adapter in analyze mode |
| Phase 7 · Localization | Rank where a fault started, not just where it showed up | Contract boundaries + fault labels already recorded |
| Phase 10 · Streaming | Traces too large for memory | Rules rewritten as incremental state machines |
