"""Command-line entry point.

    crosslayer run EXPERIMENT [--out DIR] [--seed N]
    crosslayer suite SUITE [--out DIR]   experiments in dependency order, with expected verdicts
    crosslayer replay BUNDLE [--out DIR]   re-run a bundle's configuration and compare hashes
    crosslayer compare BASELINE_BUNDLE CANDIDATE_BUNDLE [--json]
    crosslayer adapters [--json] [--check]
    crosslayer check FILE...          validate contract or experiment files
    crosslayer test-contracts FILE... run contract tests (known-good / known-bad traces); --json
    crosslayer lint FILE...           static checks; --adapter for catalog checks, --strict, --json
    crosslayer schema [NAME]          JSON Schemas for editors (--write DIR)
    crosslayer diff-contracts OLD NEW per-owner changelog; fails on unversioned semantic changes
    crosslayer new adapter NAME       scaffold a working adapter project (--dir DIR)
    crosslayer verify BUNDLE          check bundle files against manifest hashes

Exit codes: 0 pass, 1 fail/regression, 2 inconclusive, 3 error, 4 config/usage error.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from crosslayer.core.adapters.conformance import check_adapter_conformance
from crosslayer.core.adapters.registry import available_adapters, load_adapter
from crosslayer.core.artifacts.bundle import load_manifest, verify_bundle
from crosslayer.core.contracts.loader import load_contracts_file, read_document
from crosslayer.core.contracts.lint import LintLevel
from crosslayer.core.contracts.testing import load_contract_tests, run_contract_tests
from crosslayer.core.contracts.versioning import diff_contracts, render_changelog
from crosslayer.core.orchestrator.lint import lint_contract_file, lint_experiment
from crosslayer.core.errors import ConfigError
from crosslayer.core.orchestrator.compare import compare_bundles, render_comparison
from crosslayer.core.orchestrator.experiment import load_experiment
from crosslayer.core.orchestrator.runner import ExperimentRunner
from crosslayer.core.orchestrator.suite import SuiteRunner, load_suite
from crosslayer.core.schemas import SCHEMAS
from crosslayer.scaffold import create_adapter_project
from crosslayer.core.verdict import EXIT_CODES, EXIT_CONFIG_ERROR


def _cmd_run(args: argparse.Namespace) -> int:
    experiment = load_experiment(args.experiment)
    if args.seed is not None:
        experiment = experiment.with_seed(args.seed)
    runner = ExperimentRunner(args.out)
    if args.dry_run:
        plan = runner.plan(experiment)
        print(f"experiment {experiment.name}")
        print(f"adapter    {experiment.adapter}")
        print(f"mode       {plan.mode}")
        faults = ", ".join(f["name"] for f in plan.fault_labels) or "none"
        print(f"faults     {faults}")
        print(f"contracts  {len(plan.contracts)}")
        for contract in plan.contracts:
            print(f"  {contract.id}")
        print("dry run: nothing was executed and no bundle was written")
        return 0
    result = runner.run(experiment)
    m = result.manifest

    print(f"run       {result.run_id}")
    print(f"verdict   {result.verdict.upper()}")
    print(f"events    {m['events']['count']} (fingerprint {m['events']['fingerprint'][:16]})")
    if m["faults"]:
        print("faults    " + ", ".join(f["name"] for f in m["faults"]))
    for err in m["errors"]:
        print(f"error     [{err['stage']}] {err['message']}")
    if not args.quiet:
        print()
        width = max((len(r.contract.id) for r in result.results), default=8)
        for r in result.results:
            print(f"  {r.verdict.upper():<12} {r.contract.id:<{width}}  {r.summary}")
        print()
    print(f"bundle    {result.bundle_dir}")
    return EXIT_CODES[result.verdict]


def _cmd_replay(args: argparse.Namespace) -> int:
    original = load_manifest(args.bundle)
    config = args.bundle / "configuration.yaml"
    if not config.is_file():
        raise ConfigError("bundle has no configuration.yaml", location=str(args.bundle))
    out = args.out if args.out is not None else args.bundle.resolve().parent / "replays"
    result = ExperimentRunner(out).run(load_experiment(config))
    m = result.manifest
    same_config = m["config_hash"] == original["config_hash"]
    same_events = m["events"]["fingerprint"] == original["events"]["fingerprint"]
    print(f"original  {original['run_id']}  verdict={original['verdict']}")
    print(f"replay    {result.run_id}  verdict={result.verdict}")
    print(f"config hash        {'match' if same_config else 'DIFFERENT'}")
    print(f"event fingerprint  {'match' if same_events else 'DIFFERENT'}")
    print(f"bundle    {result.bundle_dir}")
    return 0 if same_config and same_events else 1


def _cmd_compare(args: argparse.Namespace) -> int:
    comparison = compare_bundles(args.baseline, args.candidate)
    if args.json:
        print(json.dumps(comparison.to_dict(), indent=2))
    else:
        print(render_comparison(comparison))
    return 1 if comparison.regressions else 0


def _cmd_adapters(args: argparse.Namespace) -> int:
    rows = []
    failed = False
    for name, target in sorted(available_adapters().items()):
        try:
            adapter = load_adapter(name)
            row = {
                "name": name,
                "target": target,
                "capabilities": sorted(adapter.capabilities()),
                "faults": [f.name for f in adapter.faults()],
                "description": adapter.describe().description,
            }
            if args.check:
                row["problems"] = check_adapter_conformance(adapter)
                failed = failed or bool(row["problems"])
            rows.append(row)
        except Exception as exc:  # noqa: BLE001 - one broken plugin must not hide the others
            rows.append({"name": name, "target": target, "error": str(exc)})
            failed = failed or args.check
    if args.json:
        print(json.dumps(rows, indent=2))
        return 1 if failed else 0
    if not rows:
        print("no adapters registered (is the package installed?)")
    for row in rows:
        print(f"{row['name']}  ({row['target']})")
        if "error" in row:
            print(f"  failed to load: {row['error']}")
            continue
        print(f"  capabilities: {', '.join(row['capabilities'])}")
        if row["faults"]:
            print(f"  faults: {', '.join(row['faults'])}")
        if row["description"]:
            print(f"  {row['description']}")
        if args.check:
            for problem in row.get("problems", []):
                print(f"  problem: {problem}")
            if not row.get("problems"):
                print("  conformance: ok")
    return 1 if failed else 0


def _file_kind(doc: object) -> str:
    """Tell CrossLayer's YAML file types apart by their top-level keys."""
    if isinstance(doc, dict):
        if "experiments" in doc:
            return "suite"
        if "cases" in doc:
            return "contract-tests"
        if "seed" in doc or "adapter" in doc:
            return "experiment"
    return "contracts"


def _cmd_check(args: argparse.Namespace) -> int:
    status = 0
    for path in args.files:
        try:
            kind = _file_kind(read_document(path))
            if kind == "suite":
                detail = f"suite, {len(load_suite(path).entries)} experiments"
            elif kind == "contract-tests":
                detail = f"contract tests, {len(load_contract_tests(path)[1])} cases"
            elif kind == "experiment":
                load_experiment(path)
                detail = "experiment"
            else:
                detail = f"{len(load_contracts_file(path))} contracts"
            print(f"ok  {path} ({detail})")
        except (ConfigError, OSError, ValueError) as exc:
            print(f"bad {exc}")
            status = EXIT_CONFIG_ERROR
    return status


def _cmd_test_contracts(args: argparse.Namespace) -> int:
    failed = 0
    uncovered_total: list[str] = []
    files_out: list[dict] = []
    for path in args.files:
        report = run_contract_tests(path)
        uncovered = report.uncovered()
        if args.json:
            files_out.append({
                "file": str(path),
                "cases": [{
                    "contract_id": r.case.contract_id,
                    "name": r.case.name,
                    "expect": r.case.expect.value,
                    "actual": r.actual.value,
                    "passed": r.passed,
                    "problems": r.problems,
                } for r in report.results],
                "coverage": {
                    "covered": len(report.contracts) - len(uncovered),
                    "total": len(report.contracts),
                    "uncovered": uncovered,
                },
            })
        else:
            print(path)
            for r in report.results:
                mark = "ok  " if r.passed else "FAIL"
                print(f"  {mark} [{r.case.contract_id}] {r.case.name}")
                for problem in r.problems:
                    print(f"         {problem}")
            covered = len(report.contracts) - len(uncovered)
            print(f"  coverage: {covered}/{len(report.contracts)} contracts have a passing known-good and known-bad case")
            for cid in uncovered:
                have = sorted(v.value for v in report.coverage()[cid]) or ["none"]
                print(f"    missing: {cid} (demonstrated: {', '.join(have)})")
        failed += sum(not r.passed for r in report.results)
        uncovered_total += uncovered
    status = 1 if failed or (args.require_coverage and uncovered_total) else 0
    if args.json:
        print(json.dumps({"passed": status == 0, "failed_cases": failed, "files": files_out}, indent=2))
    else:
        print("all cases passed" if not failed else f"{failed} case(s) failed")
    return status


def _cmd_lint(args: argparse.Namespace) -> int:
    targets: list[tuple[str, list]] = []
    for path in args.files:
        kind = _file_kind(read_document(path))
        if kind == "suite":
            for entry in load_suite(path).entries:
                targets.append((f"{path} [{entry.id}]", lint_experiment(entry.experiment)))
        elif kind == "experiment":
            targets.append((str(path), lint_experiment(load_experiment(path))))
        elif kind == "contract-tests":
            if not args.json:
                print(f"{path}: contract tests are checked with `crosslayer test-contracts`")
        else:
            targets.append((str(path), lint_contract_file(path, args.adapter)))

    worst = 0
    for label, findings in targets:
        if not args.json:
            print(f"{label}: {len(findings)} finding(s)")
            for finding in findings:
                print(f"  {finding}")
        if any(f.level is LintLevel.ERROR for f in findings):
            worst = 1
        elif args.strict and any(f.level is LintLevel.WARNING for f in findings):
            worst = 1
    if args.json:
        doc = {"passed": worst == 0, "targets": [{
            "target": label,
            "findings": [{"code": f.code, "level": f.level.value, "contract_id": f.contract_id,
                          "message": f.message} for f in findings],
        } for label, findings in targets]}
        print(json.dumps(doc, indent=2))
    return worst


def _cmd_suite(args: argparse.Namespace) -> int:
    suite = load_suite(args.suite)
    print(f"suite     {suite.name} ({len(suite.entries)} experiments)")

    def show(r) -> None:
        verdict = r.verdict.upper() if r.verdict else "-"
        detail = f"blocked by {', '.join(r.blocked_by)}" if r.blocked_by else (r.run_id or "")
        print(f"  {r.status.upper():<8} {r.entry.id:<24} expect {r.entry.expect.upper():<12} got {verdict:<12} {detail}")

    outcome = SuiteRunner(args.out).run(suite, on_result=show)
    print(f"result    {'PASSED' if outcome.passed else 'FAILED'}")
    print(f"output    {outcome.suite_dir}")
    return 0 if outcome.passed else 1


def _cmd_diff_contracts(args: argparse.Namespace) -> int:
    changes = diff_contracts(load_contracts_file(args.old), load_contracts_file(args.new))
    if args.markdown:
        print(render_changelog(changes), end="")
    else:
        if not changes:
            print("no contract changes")
        for c in changes:
            flag = f"  PROBLEM: {c.problem}" if c.problem else ""
            print(f"{c.owner:<16} {c.contract_id:<36} {c.describe()}{flag}")
    return 1 if any(c.problem for c in changes) else 0


def _cmd_new(args: argparse.Namespace) -> int:
    root = create_adapter_project(args.name, args.dir)
    print(f"created {root}")
    print("next:")
    print(f"  cd {root}")
    print("  pip install -e .")
    print("  pytest                       # passes before any edits")
    print(f"  then follow {root / 'README.md'}")
    return 0


def _schema_text(name: str) -> str:
    return json.dumps(SCHEMAS[name](), indent=2) + "\n"


def _cmd_schema(args: argparse.Namespace) -> int:
    if args.write:
        args.write.mkdir(parents=True, exist_ok=True)
        for name in SCHEMAS:
            path = args.write / f"{name}.schema.json"
            path.write_text(_schema_text(name), encoding="utf-8", newline="\n")
            print(f"wrote {path}")
        return 0
    if args.name is None:
        print("available schemas: " + ", ".join(SCHEMAS))
        return 0
    print(_schema_text(args.name), end="")
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    problems = verify_bundle(args.bundle)
    for problem in problems:
        print(problem)
    print("bundle intact" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


def _installed_version() -> str:
    from importlib import metadata

    try:
        return metadata.version("crosslayer")
    except metadata.PackageNotFoundError:
        return "unknown"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="crosslayer", description="Cross-stack verification platform")
    parser.add_argument("--version", action="version", version=f"crosslayer {_installed_version()}")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run an experiment and write a run bundle")
    run.add_argument("experiment", type=Path)
    run.add_argument("--out", type=Path, default=Path("runs"), help="directory for run bundles (default: runs/)")
    run.add_argument("--seed", type=int, help="override the experiment seed")
    run.add_argument("--dry-run", action="store_true", help="validate and print the plan without running")
    run.add_argument("--quiet", action="store_true", help="do not list per-contract results")
    run.set_defaults(func=_cmd_run)

    suite_p = sub.add_parser("suite", help="run a suite of experiments in dependency order")
    suite_p.add_argument("suite", type=Path)
    suite_p.add_argument("--out", type=Path, default=Path("runs"), help="output directory (default: runs/)")
    suite_p.set_defaults(func=_cmd_suite)

    replay = sub.add_parser("replay", help="re-run a bundle's configuration and compare hashes")
    replay.add_argument("bundle", type=Path)
    replay.add_argument("--out", type=Path, help="directory for the new bundle (default: replays/ beside the bundle)")
    replay.set_defaults(func=_cmd_replay)

    compare = sub.add_parser("compare", help="compare two run bundles")
    compare.add_argument("baseline", type=Path)
    compare.add_argument("candidate", type=Path)
    compare.add_argument("--json", action="store_true")
    compare.set_defaults(func=_cmd_compare)

    adapters = sub.add_parser("adapters", help="list registered adapters")
    adapters.add_argument("--json", action="store_true")
    adapters.add_argument("--check", action="store_true", help="run conformance checks; exit 1 on any problem")
    adapters.set_defaults(func=_cmd_adapters)

    check = sub.add_parser("check", help="validate contract or experiment files")
    check.add_argument("files", type=Path, nargs="+")
    check.set_defaults(func=_cmd_check)

    tests = sub.add_parser("test-contracts", help="run contract tests (known-good / known-bad traces)")
    tests.add_argument("files", type=Path, nargs="+")
    tests.add_argument("--require-coverage", action="store_true",
                       help="fail unless every contract has a passing known-good and known-bad case")
    tests.add_argument("--json", action="store_true", help="print one JSON document instead of text")
    tests.set_defaults(func=_cmd_test_contracts)

    lint = sub.add_parser("lint", help="statically check contract or experiment files")
    lint.add_argument("files", type=Path, nargs="+")
    lint.add_argument("--adapter", help="check contract files against this adapter's catalog and subsystems")
    lint.add_argument("--strict", action="store_true", help="treat warnings as failures")
    lint.add_argument("--json", action="store_true", help="print one JSON document instead of text")
    lint.set_defaults(func=_cmd_lint)

    diff = sub.add_parser("diff-contracts", help="changelog between two contract files, per owner")
    diff.add_argument("old", type=Path)
    diff.add_argument("new", type=Path)
    diff.add_argument("--markdown", action="store_true", help="print a Markdown changelog grouped by owner")
    diff.set_defaults(func=_cmd_diff_contracts)

    new = sub.add_parser("new", help="scaffold a new adapter project")
    new.add_argument("kind", choices=["adapter"])
    new.add_argument("name", help="snake_case adapter name, e.g. rv32i_core")
    new.add_argument("--dir", type=Path, default=Path("."), help="parent directory (default: current)")
    new.set_defaults(func=_cmd_new)

    schema = sub.add_parser("schema", help="print or write JSON Schemas for editor validation")
    schema.add_argument("name", nargs="?", choices=sorted(SCHEMAS))
    schema.add_argument("--write", type=Path, metavar="DIR", help="write every schema into DIR")
    schema.set_defaults(func=_cmd_schema)

    verify = sub.add_parser("verify", help="verify a bundle against its manifest hashes")
    verify.add_argument("bundle", type=Path)
    verify.set_defaults(func=_cmd_verify)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
