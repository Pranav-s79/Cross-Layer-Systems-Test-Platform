"""Command-line entry point.

    crosslayer run EXPERIMENT [--out DIR] [--seed N]
    crosslayer compare BASELINE_BUNDLE CANDIDATE_BUNDLE [--json]
    crosslayer adapters [--json]
    crosslayer check FILE...          validate contract or experiment files
    crosslayer verify BUNDLE          check bundle files against manifest hashes

Exit codes: 0 pass, 1 fail/regression, 2 inconclusive, 3 error, 4 config/usage error.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from crosslayer.core.adapters.registry import available_adapters, load_adapter
from crosslayer.core.artifacts.bundle import verify_bundle
from crosslayer.core.contracts.loader import parse_contracts_document, read_document
from crosslayer.core.errors import ConfigError
from crosslayer.core.orchestrator.compare import compare_bundles, render_comparison
from crosslayer.core.orchestrator.experiment import load_experiment
from crosslayer.core.orchestrator.runner import ExperimentRunner
from crosslayer.core.verdict import EXIT_CODES, EXIT_CONFIG_ERROR


def _cmd_run(args: argparse.Namespace) -> int:
    experiment = load_experiment(args.experiment)
    if args.seed is not None:
        experiment = experiment.with_seed(args.seed)
    result = ExperimentRunner(args.out).run(experiment)
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


def _cmd_compare(args: argparse.Namespace) -> int:
    comparison = compare_bundles(args.baseline, args.candidate)
    if args.json:
        print(json.dumps(comparison.to_dict(), indent=2))
    else:
        print(render_comparison(comparison))
    return 1 if comparison.regressions else 0


def _cmd_adapters(args: argparse.Namespace) -> int:
    rows = []
    for name, target in sorted(available_adapters().items()):
        try:
            adapter = load_adapter(name)
            rows.append(
                {
                    "name": name,
                    "target": target,
                    "capabilities": sorted(adapter.capabilities()),
                    "faults": [f.name for f in adapter.faults()],
                    "description": adapter.describe().description,
                }
            )
        except Exception as exc:  # noqa: BLE001 - one broken plugin must not hide the others
            rows.append({"name": name, "target": target, "error": str(exc)})
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
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
    return 0


def _cmd_check(args: argparse.Namespace) -> int:
    status = 0
    for path in args.files:
        try:
            doc = read_document(path)
            if isinstance(doc, dict) and "adapter" in doc:
                load_experiment(path)
                print(f"ok  {path} (experiment)")
            else:
                contracts = parse_contracts_document(doc, str(path))
                print(f"ok  {path} ({len(contracts)} contracts)")
        except (ConfigError, OSError, ValueError) as exc:
            print(f"bad {exc}")
            status = EXIT_CONFIG_ERROR
    return status


def _cmd_verify(args: argparse.Namespace) -> int:
    problems = verify_bundle(args.bundle)
    for problem in problems:
        print(problem)
    print("bundle intact" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="crosslayer", description="Cross-stack verification platform")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run an experiment and write a run bundle")
    run.add_argument("experiment", type=Path)
    run.add_argument("--out", type=Path, default=Path("runs"), help="directory for run bundles (default: runs/)")
    run.add_argument("--seed", type=int, help="override the experiment seed")
    run.add_argument("--quiet", action="store_true", help="do not list per-contract results")
    run.set_defaults(func=_cmd_run)

    compare = sub.add_parser("compare", help="compare two run bundles")
    compare.add_argument("baseline", type=Path)
    compare.add_argument("candidate", type=Path)
    compare.add_argument("--json", action="store_true")
    compare.set_defaults(func=_cmd_compare)

    adapters = sub.add_parser("adapters", help="list registered adapters")
    adapters.add_argument("--json", action="store_true")
    adapters.set_defaults(func=_cmd_adapters)

    check = sub.add_parser("check", help="validate contract or experiment files")
    check.add_argument("files", type=Path, nargs="+")
    check.set_defaults(func=_cmd_check)

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
