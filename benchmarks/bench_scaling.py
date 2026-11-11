"""Check that each rule kind scales roughly linearly with trace size.

    python benchmarks/bench_scaling.py [--pairs 5000] [--repeat 5]

Evaluates every rule kind on n and 4n request/response pairs and prints the
time ratio. Linear work gives a ratio near 4; a ratio near 16 means a
quadratic path has returned. This is a benchmark, not a test, because timings
are noisy on shared machines. Run it from the repository root.
"""

from __future__ import annotations

import argparse
import platform
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bench_core import RULES, median_time, synthetic_trace  # noqa: E402

from crosslayer.core.contracts import evaluate_contract, parse_contract  # noqa: E402

GROWTH = 4
WARN_RATIO = 8.0  # halfway (geometrically) between linear (4) and quadratic (16)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pairs", type=int, default=5_000, help="base size n")
    parser.add_argument("--repeat", type=int, default=5)
    args = parser.parse_args()
    small, large = synthetic_trace(args.pairs), synthetic_trace(args.pairs * GROWTH)
    print(f"platform: {platform.platform()} | Python {platform.python_version()}")
    print(f"events: n={len(small)}, {GROWTH}n={len(large)}; median of {args.repeat} runs\n")
    print(f"{'rule':<12} {'n (s)':>9} {'4n (s)':>9} {'ratio':>7}")
    suspect = []
    for kind, rule in RULES.items():
        contract = parse_contract(
            {"id": f"bench.{kind}", "title": kind, "owner": "bench", "source": "client",
             "destination": "server", "rule": rule},
            "bench",
        )
        t_small = median_time(lambda: evaluate_contract(contract, small), args.repeat)
        t_large = median_time(lambda: evaluate_contract(contract, large), args.repeat)
        ratio = t_large / t_small if t_small else float("inf")
        flag = "  <- superlinear" if ratio > WARN_RATIO else ""
        print(f"{kind:<12} {t_small:>9.4f} {t_large:>9.4f} {ratio:>7.1f}{flag}")
        if flag:
            suspect.append(kind)
    print()
    if suspect:
        print(f"superlinear: {', '.join(suspect)} (ratio > {WARN_RATIO:g}; ~{GROWTH} is linear)")
        return 1
    print(f"all ratios <= {WARN_RATIO:g} (~{GROWTH} is linear)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
