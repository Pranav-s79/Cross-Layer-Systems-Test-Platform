"""Measure contract-evaluation throughput and end-to-end orchestration overhead.

    python benchmarks/bench_core.py [--repeat 5]

Two measurements:

1. Evaluation throughput: synthetic request/response traces of increasing
   size, evaluated against one contract per rule kind. Reports the median
   wall time per contract and events per second.
2. Orchestration overhead: median wall time of running the reference
   simulator directly versus a full ``crosslayer run`` of the same
   experiment (process launch + parsing + evaluation + bundle writing).

Numbers depend on the machine; the script prints the platform it ran on.
"""

from __future__ import annotations

import argparse
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from crosslayer.core.contracts import evaluate_contract, parse_contract
from crosslayer.core.orchestrator import ExperimentRunner, load_experiment
from crosslayer.core.telemetry import NormalizedEvent
from crosslayer.integrations.sensor_pipeline.adapter import SIM_PATH

ROOT = Path(__file__).resolve().parents[1]

RULES = {
    "count": {"kind": "count", "select": {"event_type": "req"}, "min": 1},
    "response": {
        "kind": "response",
        "trigger": {"event_type": "req"},
        "response": {"event_type": "ack"},
        "correlate_by": "txn",
        "within_ms": 5,
    },
    "transitions": {
        "kind": "transitions",
        "select": {"event_type": "*"},
        "state": "event_type",
        "group_by": "txn",
        "allowed": {"req": ["ack"], "ack": []},
    },
    "threshold": {
        "kind": "threshold",
        "select": {"event_type": "ack"},
        "attribute": "bench.latency_us",
        "aggregate": "p99",
        "op": "<=",
        "value": 5000,
    },
    "invariant": {
        "kind": "invariant",
        "select": {"event_type": "ack"},
        "expect": 'e.attributes["bench.latency_us"] >= 0',
    },
    "successive": {
        "kind": "successive",
        "select": {"event_type": "req"},
        "expect": 'curr.attributes["bench.seq"] == prev.attributes["bench.seq"] + 1',
    },
}


def synthetic_trace(pairs: int) -> list[NormalizedEvent]:
    events = []
    for i in range(pairs):
        t = i * 10_000
        events.append(NormalizedEvent("bench", f"r{i}", "bench", "client", t, "req",
                                      correlation_ids={"txn": i}, attributes={"bench.seq": i}))
        events.append(NormalizedEvent("bench", f"a{i}", "bench", "server", t + 3_000, "ack",
                                      correlation_ids={"txn": i}, attributes={"bench.latency_us": 3}))
    events.sort(key=lambda e: e.timestamp_ns)
    return events


def median_time(fn, repeat: int) -> float:
    samples = []
    for _ in range(repeat):
        start = time.perf_counter()
        fn()
        samples.append(time.perf_counter() - start)
    return statistics.median(samples)


def bench_evaluation(repeat: int) -> None:
    print("## Contract evaluation (median of", repeat, "runs)\n")
    print(f"{'rule':<12} {'events':>8} {'seconds':>9} {'events/s':>12}")
    for pairs in (1_000, 10_000, 50_000):
        events = synthetic_trace(pairs)
        for kind, rule in RULES.items():
            contract = parse_contract(
                {"id": f"bench.{kind}", "title": kind, "owner": "bench", "source": "client",
                 "destination": "server", "rule": rule},
                "bench",
            )
            result = evaluate_contract(contract, events)
            assert result.verdict == "pass", (kind, result.summary)
            seconds = median_time(lambda: evaluate_contract(contract, events), repeat)
            print(f"{kind:<12} {len(events):>8} {seconds:>9.4f} {len(events) / seconds:>12,.0f}")
    print()


def bench_overhead(repeat: int) -> None:
    experiment = load_experiment(ROOT / "experiments" / "reference" / "baseline.yaml")
    sim_cmd = [sys.executable, str(SIM_PATH), "--seed", str(experiment.seed), "--packets", "400"]

    def run_sim() -> None:
        subprocess.run(sim_cmd, stdout=subprocess.DEVNULL, check=True)

    with tempfile.TemporaryDirectory() as tmp:
        runner = ExperimentRunner(Path(tmp))

        def run_full() -> None:
            runner.run(experiment)

        sim_s = median_time(run_sim, repeat)
        full_s = median_time(run_full, repeat)
    print(f"## Orchestration overhead, reference baseline (median of {repeat} runs)\n")
    print(f"simulator alone           {sim_s:.3f} s")
    print(f"full crosslayer run       {full_s:.3f} s")
    print(f"added by crosslayer       {full_s - sim_s:.3f} s")
    print()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeat", type=int, default=5)
    args = parser.parse_args()
    print(f"platform: {platform.platform()} | {platform.processor() or platform.machine()} | "
          f"Python {platform.python_version()}\n")
    bench_evaluation(args.repeat)
    bench_overhead(args.repeat)


if __name__ == "__main__":
    main()
