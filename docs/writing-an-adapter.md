# Writing an adapter

An adapter is the only code that knows how a system is built, run, and
logged. Fastest start: `crosslayer new adapter NAME` (see [tooling.md](tooling.md)).
Reference: `crosslayer/integrations/sensor_pipeline/adapter.py`.

## 1. Describe the system

```python
from crosslayer.core.adapters import AdapterDescription, Capability, InterfaceSpec, SystemAdapter

class MyAdapter(SystemAdapter):
    def describe(self):
        return AdapterDescription(
            name="my_system",
            adapter_version="0.1.0",
            system="my-system",
            system_version="git:abc123",
            subsystems=("fetch", "decode", "regfile"),
            interfaces=(InterfaceSpec("fetch_to_decode", "fetch", "decode"),),
            external_dependencies=("Verilator 5.x",),   # listed in replay.sh
        )

    def capabilities(self):
        return frozenset({Capability.BUILD, Capability.EXECUTE,
                          Capability.COLLECT, Capability.NORMALIZE})
```

`COLLECT` and `NORMALIZE` are required. Don't implement methods for
capabilities you don't declare; the base class raises `UnsupportedOperation`.

## 2. Run and collect

* `build(ctx)` returns `BuildResult(ok, details)`; outputs go in `ctx.work_dir`.
* `execute(ctx)` runs the system with
  `crosslayer.core.orchestrator.process.run_process` and `ctx.timeout_s`
  (kills the process tree on timeout). Write raw output to `ctx.raw_dir`;
  pass `ctx.seed` to anything random.
* `collect(ctx, execution)` returns `Artifact`s under `ctx.raw_dir`. In
  log-only mode, copy `ctx.inputs` there.

## 3. Normalize

`normalize(ctx, artifacts)` returns `NormalizationResult(events, diagnostics, stats)`.

* Unique event ids (`"<file>:<line>"` works).
* `timestamp_ns` is an **int**. Parse decimals with `decimal_seconds_to_ns`, never `float`.
* Namespaced attribute keys: `rtl.pc`, `ran.rnti`.
* Identifiers go in `correlation_ids` (`{"ue": "3"}`) so rules can group by them.
* `raw_artifact_ref=ctx.raw_ref("file.log", lineno)` links findings to the line.
* Say what the timestamp means in `confidence_metadata` (`{"timestamp_source": "simulation_cycle"}`).
* Report unparsable lines as `ParseDiagnostic`s instead of dropping them.

## 4. Contracts, faults, event catalog

* `contracts()`: default contracts, usually loaded from a YAML file next to
  the adapter. Endpoints must be declared subsystems.
* `faults()`: `FaultSpec`s with their **true target** (`target_subsystem`,
  optional `target_interface`). Localization will be scored against these.
* `validate_parameters` / `validate_faults` reject bad values before a run.
* `event_catalog()` (recommended) lets `crosslayer lint` catch contracts that
  use event types or keys you never emit:

```python
from crosslayer.core.adapters import EventSpec

def event_catalog(self):
    return [
        EventSpec("rtl.fetch", ("fetch",), ("rtl.pc", "rtl.instr", "rtl.cycle"), ("hart",)),
        EventSpec("rtl.writeback", ("regfile",), ("rtl.rd", "rtl.value")),
    ]
```

Keep it truthful with `check_events_against_catalog` in your tests.

## 5. Register and test

```toml
[project.entry-points."crosslayer.adapters"]
my_system = "my_package.adapter:MyAdapter"
```

```python
from crosslayer.core.adapters import check_adapter_conformance

def test_conforms():
    assert check_adapter_conformance(MyAdapter()) == []
```

`crosslayer adapters --check` runs the same check on every registered adapter
and exits 1 if any has a problem or fails to load.

Then add contract tests (one passing and one failing trace per contract), a
baseline experiment that must PASS, and at least one fault that must FAIL.
