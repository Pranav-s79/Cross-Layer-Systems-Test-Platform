"""Adapter for the reference sensor-pipeline simulation.

Shows the full adapter lifecycle on a system whose faults are known:

* execute:   launch ``sim.py`` as a separate process with a timeout
* collect:   keep its stdout log, stderr, and a process record
* normalize: parse the firmware-style log into NormalizedEvents, reporting
             any line it cannot parse instead of dropping it
* faults:    map fault requests onto the simulator's ``--fault`` flags

It can also analyze an existing log (``inputs:`` in the experiment), which is
the same path a log-only integration such as a RAN log parser uses.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from crosslayer.core.adapters.base import (
    AdapterDescription,
    Artifact,
    Capability,
    EventSpec,
    ExecutionResult,
    ExecutionStatus,
    FaultRequest,
    FaultSpec,
    InterfaceSpec,
    NormalizationResult,
    ParseDiagnostic,
    RunContext,
    SystemAdapter,
)
from crosslayer.core.contracts.loader import load_contracts_file
from crosslayer.core.contracts.model import BoundaryContract
from crosslayer.core.errors import AdapterError, ConfigError
from crosslayer.core.orchestrator.process import run_process
from crosslayer.core.telemetry.events import NormalizedEvent, Severity
from crosslayer.core.units import decimal_seconds_to_ns

SIM_PATH = Path(__file__).with_name("sim.py")
CONTRACTS_PATH = Path(__file__).with_name("contracts.yaml")
SOURCE_SYSTEM = "sensor_pipeline"
LOG_NAME = "sim.log"
STDERR_NAME = "sim.stderr.log"
PROCESS_NAME = "process.json"

_LINE_RE = re.compile(
    r"^\[\s*(?P<ts>\d+\.\d{6})\]\s+(?P<subsystem>[a-z_]+):\s+(?P<verb>[A-Z]+)(?P<rest>.*)$"
)
_FIELD_RE = re.compile(r"([a-z_]+)=(\S+)")

# (subsystem, verb) -> (event_type, severity)
_EVENTS: dict[tuple[str, str], tuple[str, Severity]] = {
    ("sys", "BOOT"): ("system.boot", Severity.INFO),
    ("sys", "HALT"): ("system.halt", Severity.INFO),
    ("sensor_rx", "RX"): ("packet.received", Severity.INFO),
    ("sensor_rx", "DROP"): ("packet.dropped", Severity.WARNING),
    ("proc", "DONE"): ("packet.processed", Severity.INFO),
}


def _hex(value: str) -> int:
    return int(value, 16)


# raw field -> (namespaced attribute, converter)
_FIELDS: dict[str, tuple[str, Callable[[str], Any]]] = {
    "seq": ("pipeline.seq", int),
    "len": ("pipeline.len", int),
    "crc": ("pipeline.crc_rx", _hex),
    "crc_rx": ("pipeline.crc_rx", _hex),
    "crc_calc": ("pipeline.crc_calc", _hex),
    "lat_us": ("pipeline.latency_us", int),
    "reason": ("pipeline.drop_reason", str),
    "occ": ("pipeline.occupancy", int),
    "fw": ("pipeline.firmware", str),
    "seed": ("pipeline.seed", int),
    "cap": ("pipeline.buffer_capacity", int),
    "rx": ("pipeline.rx", int),
    "done": ("pipeline.done", int),
    "drop": ("pipeline.dropped", int),
}

_PARAMETERS: dict[str, tuple[str, int]] = {
    # experiment parameter -> (simulator flag, default)
    "packets": ("--packets", 400),
    "buffer_capacity": ("--buffer-capacity", 8),
    "mean_interarrival_us": ("--mean-interarrival-us", 250),
    "service_us_min": ("--service-us-min", 100),
    "service_us_max": ("--service-us-max", 300),
}

_FAULT_PARAMS: dict[str, dict[str, tuple[str, int]]] = {
    # fault -> param -> (simulator flag, default)
    "silent_drop": {},
    "slow_consumer": {"factor": ("--slow-factor", 3)},
    "corrupt_checksum": {"every": ("--corrupt-every", 25)},
    "duplicate_completion": {"every": ("--duplicate-every", 40)},
    "reorder": {"every": ("--reorder-every", 10)},
}


def _positive_int(value: Any, location: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigError("must be a positive integer", location=location)
    return value


class SensorPipelineAdapter(SystemAdapter):
    def describe(self) -> AdapterDescription:
        digest = hashlib.sha256(SIM_PATH.read_bytes()).hexdigest()[:12]
        return AdapterDescription(
            name="sensor_pipeline",
            adapter_version="0.1.0",
            system="crosslayer-reference-sensor-pipeline",
            system_version=f"sim.py sha256:{digest}",
            subsystems=("sensor", "sensor_rx", "ring_buffer", "proc", "sys", "host"),
            interfaces=(
                InterfaceSpec("sensor_link", "sensor", "sensor_rx", "packets arriving from the sensor"),
                InterfaceSpec("rx_to_buffer", "sensor_rx", "ring_buffer", "RX path enqueues or drops"),
                InterfaceSpec("buffer_to_proc", "ring_buffer", "proc", "consumer dequeues packets"),
            ),
            description="Simulated sensor/ring-buffer/consumer pipeline used to validate the core (not real firmware).",
            external_dependencies=(f"Python {sys.version_info.major}.{sys.version_info.minor}+ to run sim.py",),
        )

    def capabilities(self) -> frozenset[Capability]:
        return frozenset(
            {Capability.EXECUTE, Capability.COLLECT, Capability.NORMALIZE, Capability.FAULT_INJECTION}
        )

    def faults(self) -> list[FaultSpec]:
        return [
            FaultSpec(
                "silent_drop",
                "Buffer-overflow path discards packets without recording the drop.",
                target_subsystem="sensor_rx",
                target_interface="rx_to_buffer",
            ),
            FaultSpec(
                "slow_consumer",
                "Consumer service time multiplied by `factor`, so the buffer saturates.",
                target_subsystem="proc",
                parameters={"factor": "service-time multiplier (default 3)"},
            ),
            FaultSpec(
                "corrupt_checksum",
                "One bit flipped in every Nth packet while it waits in the ring buffer.",
                target_subsystem="ring_buffer",
                target_interface="buffer_to_proc",
                parameters={"every": "corrupt every Nth buffered packet (default 25)"},
            ),
            FaultSpec(
                "duplicate_completion",
                "Every Nth completion is reported twice.",
                target_subsystem="proc",
                parameters={"every": "duplicate every Nth completion (default 40)"},
            ),
            FaultSpec(
                "reorder",
                "Every Nth dequeue with two or more packets waiting takes the second packet first.",
                target_subsystem="ring_buffer",
                target_interface="buffer_to_proc",
                parameters={"every": "reorder every Nth dequeue (default 10)"},
            ),
        ]

    def contracts(self) -> list[BoundaryContract]:
        return load_contracts_file(CONTRACTS_PATH)

    def event_catalog(self) -> list[EventSpec]:
        unparsed = "pipeline.unparsed_*"  # unknown log fields are kept under this prefix
        return [
            EventSpec("system.boot", ("sys",),
                      ("pipeline.firmware", "pipeline.seed", "pipeline.buffer_capacity", unparsed)),
            EventSpec("system.halt", ("sys",),
                      ("pipeline.rx", "pipeline.done", "pipeline.dropped", unparsed)),
            EventSpec("packet.received", ("sensor_rx",),
                      ("pipeline.seq", "pipeline.len", "pipeline.crc_rx", unparsed), ("packet",)),
            EventSpec("packet.dropped", ("sensor_rx",),
                      ("pipeline.seq", "pipeline.drop_reason", "pipeline.occupancy", unparsed), ("packet",)),
            EventSpec("packet.processed", ("proc",),
                      ("pipeline.seq", "pipeline.crc_rx", "pipeline.crc_calc", "pipeline.latency_us", unparsed),
                      ("packet",)),
            EventSpec("process.exit", ("host",), ("process.exit_code",),
                      description="synthesized from the process record; timestamp inferred"),
        ]

    def validate_parameters(self, parameters: Mapping[str, Any]) -> None:
        unknown = set(parameters) - set(_PARAMETERS)
        if unknown:
            raise ConfigError(f"unknown parameters {sorted(unknown)} (allowed: {sorted(_PARAMETERS)})")
        for key, value in parameters.items():
            _positive_int(value, f"parameters.{key}")
        lo = parameters.get("service_us_min", _PARAMETERS["service_us_min"][1])
        hi = parameters.get("service_us_max", _PARAMETERS["service_us_max"][1])
        if lo > hi:
            raise ConfigError("service_us_min must be <= service_us_max", location="parameters")

    def validate_faults(self, faults: Sequence[FaultRequest]) -> None:
        for fault in faults:
            for param, value in fault.params.items():
                _positive_int(value, f"faults.{fault.name}.{param}")

    def _command(self, ctx: RunContext) -> list[str]:
        cmd = [sys.executable, str(SIM_PATH), "--seed", str(ctx.seed)]
        for key, (flag, default) in _PARAMETERS.items():
            cmd += [flag, str(ctx.parameters.get(key, default))]
        for fault in ctx.faults:
            cmd += ["--fault", fault.name]
            for param, (flag, default) in _FAULT_PARAMS[fault.name].items():
                cmd += [flag, str(fault.params.get(param, default))]
        return cmd

    def execute(self, ctx: RunContext) -> ExecutionResult:
        command = self._command(ctx)
        result = run_process(
            command,
            cwd=ctx.work_dir,
            timeout_s=ctx.timeout_s,
            stdout_path=ctx.raw_dir / LOG_NAME,
            stderr_path=ctx.raw_dir / STDERR_NAME,
            env_overrides={"PYTHONHASHSEED": "0", "CROSSLAYER_RUN_ID": ctx.run_id},
        )
        if result.error is not None:
            status = ExecutionStatus.FAILED_TO_START
        elif result.timed_out:
            status = ExecutionStatus.TIMEOUT
        else:
            status = ExecutionStatus.COMPLETED
        return ExecutionResult(
            status=status,
            exit_code=result.exit_code,
            duration_s=result.duration_s,
            command=command,
            details={"error": result.error} if result.error else {},
        )

    def collect(self, ctx: RunContext, execution: ExecutionResult | None) -> list[Artifact]:
        log = ctx.raw_dir / LOG_NAME
        if ctx.inputs:
            if len(ctx.inputs) != 1:
                raise AdapterError("analyze mode expects exactly one sim.log input")
            shutil.copyfile(ctx.inputs[0], log)
        if not log.is_file():
            raise AdapterError(f"no {LOG_NAME} was produced")
        artifacts = [Artifact(LOG_NAME, log, description="simulator log (stdout)")]

        stderr = ctx.raw_dir / STDERR_NAME
        if stderr.is_file():
            artifacts.append(Artifact(STDERR_NAME, stderr, description="simulator stderr"))
        if execution is not None:
            record = ctx.raw_dir / PROCESS_NAME
            record.write_text(json.dumps(execution.to_dict(), indent=2) + "\n", encoding="utf-8")
            artifacts.append(Artifact(PROCESS_NAME, record, "application/json", "process exit record"))
        return artifacts

    def normalize(self, ctx: RunContext, artifacts: Sequence[Artifact]) -> NormalizationResult:
        by_name = {a.name: a for a in artifacts}
        events: list[NormalizedEvent] = []
        diagnostics: list[ParseDiagnostic] = []
        lines = 0

        with by_name[LOG_NAME].path.open("r", encoding="utf-8") as fh:
            for lineno, raw in enumerate(fh, start=1):
                line = raw.rstrip("\r\n")
                if not line.strip():
                    continue
                lines += 1
                event, problems = self._parse_line(ctx, line, lineno)
                diagnostics += [ParseDiagnostic(LOG_NAME, lineno, p) for p in problems]
                if event is not None:
                    events.append(event)

        if PROCESS_NAME in by_name:
            record = json.loads(by_name[PROCESS_NAME].path.read_text(encoding="utf-8"))
            if record["exit_code"] is not None:
                events.append(
                    NormalizedEvent(
                        run_id=ctx.run_id,
                        event_id=f"{PROCESS_NAME}:exit",
                        source_system=SOURCE_SYSTEM,
                        subsystem="host",
                        timestamp_ns=max((e.timestamp_ns for e in events), default=0),
                        event_type="process.exit",
                        attributes={"process.exit_code": record["exit_code"]},
                        raw_artifact_ref=ctx.raw_ref(PROCESS_NAME),
                        confidence_metadata={"timestamp_source": "inferred_end_of_log"},
                    )
                )

        return NormalizationResult(
            events=events,
            diagnostics=diagnostics,
            stats={"log_lines": lines, "events": len(events), "diagnostics": len(diagnostics)},
        )

    def _parse_line(self, ctx: RunContext, line: str, lineno: int) -> tuple[NormalizedEvent | None, list[str]]:
        match = _LINE_RE.match(line)
        if match is None:
            return None, [f"unrecognized line: {line[:120]!r}"]
        key = (match["subsystem"], match["verb"])
        if key not in _EVENTS:
            return None, [f"unknown record {key[0]}:{key[1]}"]
        event_type, severity = _EVENTS[key]

        problems: list[str] = []
        attributes: dict[str, Any] = {}
        for name, value in _FIELD_RE.findall(match["rest"]):
            if name not in _FIELDS:
                problems.append(f"unknown field {name!r} kept as text")
                attributes[f"pipeline.unparsed_{name}"] = value
                continue
            attr, convert = _FIELDS[name]
            try:
                attributes[attr] = convert(value)
            except ValueError:
                return None, [f"bad value {name}={value!r}"]

        correlation = {"packet": attributes["pipeline.seq"]} if "pipeline.seq" in attributes else {}
        event = NormalizedEvent(
            run_id=ctx.run_id,
            event_id=f"{LOG_NAME}:{lineno}",
            source_system=SOURCE_SYSTEM,
            subsystem=match["subsystem"],
            timestamp_ns=decimal_seconds_to_ns(match["ts"]),
            event_type=event_type,
            severity=severity,
            correlation_ids=correlation,
            attributes=attributes,
            raw_artifact_ref=ctx.raw_ref(LOG_NAME, lineno),
            confidence_metadata={"timestamp_source": "sut_simulated_clock", "timestamp_resolution_ns": 1000},
        )
        return event, problems
