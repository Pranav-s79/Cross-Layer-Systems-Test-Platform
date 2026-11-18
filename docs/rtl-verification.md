# RTL processor verification (RV32I)

The `rv32i` adapter runs an unmodified RV32I core with Icarus Verilog,
normalizes its RISC-V Formal Interface (RVFI) retirement trace, and compares
every retired instruction with an independent RV32I instruction-set model.

| Core | Upstream (pinned commit) | License | Trace | Suite |
|---|---|---|---|---|
| `picorv32` (default) | YosysHQ PicoRV32 `ef203c2b` | ISC | RVFI port (`rvfi`, `commit-log`, `vcd-signals`) | `experiments/rv32i/` |
| `femtorv32-quark` | BrunoLevy/learn-fpga `0a97ede5` | BSD-3-Clause | No RVFI; a testbench monitor (`vcd-signals`, `commit-log`) | `experiments/rv32i-quark/` |

Both RTL files are byte-identical to upstream (Git blobs recorded in each
core's `README.md` and `core.yaml`); mutants edit run-local copies.

### A core without RVFI

FemtoRV32 Quark has no formal interface and no traps. Its testbench
(`cores/femtorv32-quark/tb.v`) adds a retirement monitor that reads the
core's state machine through hierarchical references, without editing the
RTL. In EXECUTE it latches the PC, instruction, operands, next PC and data
access; the instruction retires at that edge, or, for loads, stores and
shifts, when WAIT_ALU_OR_MEM ends, with the final write-back value. It
applies RVFI conventions (unused operands read 0, word-aligned data
addresses) and pulses `mon_valid`, which the profile's `signals` map samples
from the waveform. The testbench ends the run when EBREAK or ECALL retires
(Quark executes them as no-ops) and reports it as a trap, like RVFI would.
It also overrides `ADDR_WIDTH` to 32 so the core can reach the output port.

The six mutants are rewritten for Quark's RTL and name the same model
faults; each first fails at the instruction the model predicts, as on
PicoRV32 (`test_rv32i_traces.py`).

## Prerequisites

Install CrossLayer and put both Icarus executables on `PATH`:

```sh
pip install -e ".[dev]"
iverilog -V
vvp -V
```

No RISC-V compiler is required. The integration includes a deliberately small
two-pass RV32I assembler for test programs.

## Run the verification flow

Start with the clean core, then run the dependency-ordered mutant suite:

```sh
crosslayer run experiments/rv32i/baseline.yaml --out runs
crosslayer suite experiments/rv32i/suite.yaml --out runs
crosslayer suite experiments/rv32i-quark/suite.yaml --out runs   # second core
```

Compiled simulations are cached by a SHA-256 of everything `iverilog`
compiles (source text after mutants, defines, flags) and the identity of the
`iverilog` and `vvp` binaries, so runs that share RTL skip compilation; the
manifest's `build.compile_cache` records the key and whether it hit. The
cache lives in `%LOCALAPPDATA%/crosslayer/rv32i-sim` (Windows) or
`~/.cache/crosslayer/rv32i-sim`; set `CROSSLAYER_CACHE_DIR` to move it, or to
`off` to disable it.

The baseline must pass all 10 contracts. Each mutant experiment declares
`expect: fail` and depends on the baseline, so a broken clean run blocks mutant
claims instead of producing misleading fault-detection results.

Every run bundle contains `report.html`. Open it locally and move through the
five numbered steps:

1. Confirm the overall verdict and run scope.
2. Check the adapter, system hash, seed, configuration, and injected fault.
3. Follow build, execute, collect, normalize, evaluate, and write timings.
4. Search or filter the 10 contract results.
5. Expand the first failure, follow its raw trace reference, and replay the run.

The report is self-contained, loads no network resources, supports Previous /
Next and left/right keyboard navigation, and leaves every section visible when
JavaScript is disabled.

## What the adapter records

The testbench provides 64 KiB of memory and prints one record per retired
instruction, in the selected [trace format](#trace-sources). The adapter
preserves and indexes:

| Artifact | Purpose |
|---|---|
| `trace.log` | Raw boot, retirement (`RET` or commit lines), output, bus-error, halt, and timeout records |
| `program.s` | Exact assembly source |
| `program.hex` | Memory image passed to `$readmemh` |
| `program.lst` | Address and machine word mapped to the source line |
| `rtl.diff` | Exact run-local RTL edit for an injected mutant |
| `process.json` | Simulator command, exit status, and duration |
| `waveform.vcd` | Top-level and core waveform (optional; always written for `vcd-signals`) |

Each normalized `cpu.retire` event includes the observed RVFI fields, the
reference model's fields at the same retirement order, the decoded operation,
the assembly source line, cycles per instruction (timed formats), and a raw
`trace.log#L…` reference. The `cpu.boot` event records the trace format and
whether it carries cycles (`rv.timed`) and operand values (`rv.has_operands`).

## Trace sources

A trace source turns a testbench's output into the same retirement records
(RVFI field names), so contracts and the reference model never depend on the
format. A profile lists the formats its testbench supports (`trace:`, first
is the default); `parameters.trace` selects one.

| Format | Produced by | Cycles | Operand values | Fields rebuilt by the parser |
|---|---|---|---|---|
| `rvfi` | `RET` lines from the RVFI port | yes | yes | none |
| `commit-log` | Spike `--log-commits` lines (`+commit_log` in `tb.v`) | no | no | byte lanes and store data from the width and byte address; next PC from the next line; an exception's instruction from the program image |
| `vcd-signals` | The waveform, through the profile's `signals` map; the testbench prints no retirement lines | if `cyc` is mapped | if `rs1v` and `rs2v` are mapped | none; unmapped fields are absent |

`vcd-signals` needs no trace printing in the RTL or testbench, only a waveform
dump. At each rising edge of `signals.clock` the reader takes the values in
effect just before the edge, as a flip-flop would; if `signals.valid` is 1,
`signals.fields` (RVFI field name to dotted signal name) form one retirement,
whose evidence points at the edge's `#time` line in `waveform.vcd`. The reader
streams the file, so memory does not grow with simulation length. Markers
still come from `trace.log`; both are merged by cycle.

Contracts that need a field a format lacks are SKIPPED, never passed:
`lockstep_operands` needs operand values and `cpi_budget` needs cycles. The
same mutants are still caught; one is caught later. `regfile_wrong_rd` writes
the wrong register, which RVFI shows at the next read of it, but a commit log
only shows it once a result depends on that value. The tests predict each
format's first failing instruction from the model, compared on the fields
that format observes. A timing-only fault (`mem_wait_states`) is invisible
without cycles, and caught through a waveform (`vcd_mem_wait_states`).

## Contracts

| Contract | Check |
|---|---|
| `rv32i.sequential_pc` | Non-control-flow instructions advance to `PC + 4` |
| `rv32i.jump_target` | Jumps and branches select a decoded legal target |
| `rv32i.lockstep_control_flow` | PC, instruction, trap, and next PC match the model |
| `rv32i.lockstep_operands` | Register operands match the model |
| `rv32i.lockstep_writeback` | Destination register and value match the model |
| `rv32i.lockstep_memory` | Address, byte lanes, and write data match the model |
| `rv32i.program_output` | Memory-mapped output values match in order |
| `rv32i.clean_halt` | The run reaches EBREAK at the expected retirement count |
| `rv32i.no_bus_errors` | No access leaves mapped memory |
| `rv32i.cpi_budget` | The 95th-percentile CPI of non-shift instructions is at most eight cycles (both cores shift in steps, so shift latency grows with the amount) |

Run the known-good and known-bad case for every contract without a simulator:

```sh
crosslayer test-contracts crosslayer/integrations/rv32i/contract_tests.yaml --require-coverage
```

## Faults

| Fault | Injection point | Expected first symptom |
|---|---|---|
| `bne_inverted` | Branch decision RTL | Wrong branch next PC |
| `sub_as_add` | ALU RTL | Wrong writeback value |
| `slt_unsigned` | Signed comparator RTL | Wrong compare result |
| `jal_imm_bit11` | JAL immediate RTL | Wrong jump target |
| `regfile_wrong_rd` | PicoRV32 `TESTBUG_001` | A later operand reads the wrong register state |
| `store_byte_lane` | Store byte-lane RTL | Wrong memory mask and data lanes |
| `mem_wait_states` | Testbench memory timing | CPI budget failure with matching functional results |

Functional faults are modeled independently in `iss.py`. Simulator-enabled
tests assert that the RTL trace first differs from the clean reference at the
same retirement order predicted by that model.

## Core profiles

Each core is a directory with a `core.yaml`, its RTL, and a testbench. The
built-in profile is [`cores/picorv32/core.yaml`](../crosslayer/integrations/rv32i/cores/picorv32/core.yaml).
Select a core with `parameters.core`: a built-in name, or a path (relative to
the experiment) to a `core.yaml` or its directory. A new core or mutant needs
no Python changes.

| Key | Meaning |
|---|---|
| `name`, `description` | Lowercase identifier; free text |
| `isa`, `trace` | `rv32i`; a [trace format](#trace-sources) or a list of them (first is the default) |
| `testbench`, `sources` | Paths inside the profile directory, compiled in this order |
| `defines` | Always passed to `iverilog` as `-D` |
| `clock_period_ns` | Converts cycles to event timestamps (default 10) |
| `provenance` | Free-form mapping recorded in the bundle (`upstream`, `commit`, `license`) |
| `signals` | For `vcd-signals`: `clock`, `valid`, and `fields` (RVFI field to signal name; `pc` and `insn` required) |
| `mutants.<name>` | `target_subsystem`, optional `target_interface`, `edits` (`file`, `find`, `replace`) and/or `defines`, optional `model_fault` |

Profiles are validated before anything runs: unknown keys, missing files,
targets the adapter does not declare, and model faults `iss.py` does not
know are config errors. Each mutant `find` must match exactly once at build
time. Only the selected core's mutants (plus `mem_wait_states`) are valid
faults for an experiment. The build records the core's name, files, SHA-256,
and provenance in the manifest, and `rtl.diff` names the core.

## Parameters

| Parameter | Default | Meaning |
|---|---:|---|
| `core` | `picorv32` | Built-in core name, or path to a `core.yaml` or its directory |
| `trace` | profile default | A trace format the core's profile lists (`rvfi`, `commit-log`, `vcd-signals`) |
| `program` | `selftest` | Built-in name, `random`, or path to a `.s` file |
| `max_cycles` | `100000` | Testbench cycle limit |
| `waveform` | `false` | Write `waveform.vcd` when true |
| `random_instructions` | `300` | Body length for `program: random` |

Random programs use the experiment seed and initialize every working
register. They mix ALU and shift instructions, LUI and AUIPC, loads and
stores of every width at every aligned byte offset of a scratch region, and
forward-only branches, JAL, and AUIPC+JALR pairs, so they always terminate.
A quarter of the branches compare a register with itself, so each branch is
taken and not taken across seeds. All working registers are XOR-folded into
the one output value; EBREAK ends the program.

## Coverage

Every run reports ISA coverage bins in `normalization.json` and `report.md`:

| Group | Bins |
|---|---|
| `mnemonic` | Each of the 39 RV32I mnemonics the model executes, retired |
| `branch` | Each branch mnemonic, taken and not taken |
| `memory` | Each load and store width at each aligned byte offset (load offsets need operand values, so they are omitted for `commit-log`) |

Both suites run `random_programs` as a 20-seed sweep, and the suite report
lists bins no seed hit. On both cores the only one is `ecall`, which ends a
run like EBREAK and so is never generated. `isa_coverage` adds
[`coverage_contracts.yaml`](../crosslayer/integrations/rv32i/coverage_contracts.yaml),
a `coverage` contract that fails if the self-test leaves any other mnemonic
unretired.

Random sweeps found two bugs the self-test hid:

* PicoRV32 reports `mem_wdata` as unknown (`x`) on a load that precedes
  every store; RVFI defines it only in written lanes. The parsers now treat
  unknown digits in unwritten lanes as don't-care (unknowns in written lanes
  are still diagnostics). Before, the line was dropped and lockstep failed
  from that instruction on.
* The old generator wrote its output through `t0`, which is `x5`, the output
  register, so every random program printed the port address.

## Scope

This phase verifies the RV32I base instruction subset exercised by the bundled
assembler and programs. It does not claim compressed instructions, multiply /
divide extensions, interrupts, caches, operating-system boot, synthesis, or
physical hardware coverage.
