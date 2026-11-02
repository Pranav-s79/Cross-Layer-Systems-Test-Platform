# Writing an adapter

An adapter is the only code that knows how a system is built, run, and
logged. The reference implementation is
`crosslayer/integrations/sensor_pipeline/adapter.py`.

## 1. Describe the system

```python
from crosslayer.core.adapters import AdapterDescription, Capability, InterfaceSpec, SystemAdapter

class MyAdapter(SystemAdapter):
    def describe(self):
        return AdapterDescription(
            name="my_system",
            adapter_version="0.1.0",
            system="my-system",
            system_version="git:abc123",          # or a content hash of the SUT
            subsystems=("fetch", "decode", "regfile"),
            interfaces=(InterfaceSpec("fetch_to_decode", "fetch", "decode"),),
            external_dependencies=("Verilator 5.x",),   # listed in replay.sh
        )

    def capabilities(self):
        return frozenset({Capability.BUILD, Capability.EXECUTE,
                          Capability.COLLECT, Capability.NORMALIZE})
```

`COLLECT` and `NORMALIZE` are required. Do **not** implement methods for
capabilities you do not declare. The base class raises
`UnsupportedOperation`, which makes "unsupported" explicit.

## 2. Run and collect

* `build(ctx)` returns `BuildResult(ok, details)`. Put outputs in `ctx.work_dir`.
* `execute(ctx)` runs the system. Use
  `crosslayer.core.orchestrator.process.run_process` with `ctx.timeout_s`.
  It kills the whole process tree on timeout. Write raw output into
  `ctx.raw_dir`, and pass `ctx.seed` to anything random.
* `collect(ctx, execution)` returns `Artifact` entries for files under
  `ctx.raw_dir`. In log-only mode (`inputs:` in the experiment), copy
  `ctx.inputs` into `ctx.raw_dir` here.

## 3. Normalize

`normalize(ctx, artifacts)` returns `NormalizationResult(events, diagnostics, stats)`.

* `run_id=ctx.run_id`. Event ids must be unique (`"<file>:<line>"` works well).
* `timestamp_ns` is an **int**. Parse decimal timestamps exactly
  (`crosslayer.core.units.decimal_seconds_to_ns`), never through `float`.
* Attribute keys are namespaced: `rtl.pc`, `ran.rnti`.
* Put identifiers in `correlation_ids` (`{"ue": "3"}`), not only in attributes,
  so `response`, `transitions`, and `successive` rules can group by them.
* `raw_artifact_ref=ctx.raw_ref("file.log", lineno)` lets every finding point
  back to the original line.
* Record what the timestamp means in `confidence_metadata`, for example
  `{"timestamp_source": "simulation_cycle"}` or `"inferred_end_of_log"`.
* Report lines you could not parse as `ParseDiagnostic`s. Do not drop them silently.

## 4. Ship contracts and faults (optional)

* `contracts()` returns default contracts, typically from a YAML file next to
  the adapter (`load_contracts_file`). Their `source`/`destination` must be
  declared subsystems.
* `faults()` returns `FaultSpec`s with the **ground-truth target**
  (`target_subsystem`, optional `target_interface`) and parameter names.
  These labels are what fault-localization accuracy will be scored against.
* `validate_parameters` and `validate_faults` reject bad values before
  anything runs.

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

Then add a baseline experiment that must PASS and at least one controlled
fault that must FAIL the contract designed to catch it.
