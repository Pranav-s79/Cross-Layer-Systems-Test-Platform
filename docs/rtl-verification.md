# RTL processor verification (RV32I)

The `rv32i` adapter runs an unmodified RV32I core with Icarus Verilog,
normalizes its RISC-V Formal Interface (RVFI) retirement trace, and compares
every retired instruction with an independent RV32I instruction-set model.

| Core | Upstream (pinned commit) | License | Trace | Suite |
|---|---|---|---|---|
| `picorv32` (default) | YosysHQ PicoRV32 `ef203c2b` | ISC | RVFI port (`rvfi`, `commit-log`, `vcd-signals`) | `experiments/rv32i/` |
| `femtorv32-quark` | BrunoLevy/learn-fpga `0a97ede5` | BSD-3-Clause | No RVFI; a testbench monitor (`vcd-signals`, `commit-log`) | `experiments/rv32i-quark/` |
| `learnfpga-pipeline` | BrunoLevy/learn-fpga `f5ada24a` | BSD-3-Clause | No RVFI; a pipeline monitor (`rvfi`, `commit-log`, `vcd-signals`) | `experiments/rv32i-pipeline/` |

PicoRV32 and Quark are multi-cycle: each instruction finishes before the
next starts. `learnfpga-pipeline` is a five-stage pipeline with forwarding,
a load-use stall, and branch flushes, so it can get hazards wrong. All three
cores' RTL is byte-identical to upstream (Git blobs recorded in each core's
`README.md` and `core.yaml`); mutants edit run-local copies.

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

### A pipelined core

`learnfpga-pipeline` is step 6 of Bruno Levy's pipelining tutorial
(`pipeline6.v`): fetch, decode, execute, memory, write-back. Execute
receives results forwarded from memory and write-back; an instruction that
uses the result of the load just ahead of it stalls fetch and decode for a
cycle; jumps and branches resolve in execute, and a taken one flushes the
two instructions fetched behind it. [The core's README](../crosslayer/integrations/rv32i/cores/learnfpga-pipeline/README.md)
records the candidates considered and why this one was chosen.

Its testbench monitor reads the pipeline registers. It captures each
instruction's operands in execute, after forwarding (the values the ALU
actually used), and carries them in two shadow stages, which works because
the core never stalls execute, memory, or write-back. An instruction
retires when it leaves write-back, unless it is the core's bubble
(`add x0, x0, x0`); the core's own `instret` counter uses the same rule. The
monitor prints RVFI-style `RET` lines, so all three trace formats work. The
core has separate instruction and data memories that it loads itself from
fixed file names (`memory_images` in the profile); both get the program
image. It has no memory handshake, so `mem_wait_states` gates its clock
instead: every core cycle takes N+1 testbench cycles.

Four mutants break the hazard logic, and the reference model predicts each
one independently (`PIPELINE_FAULTS` in `iss.py`). The model tracks pipeline
state only when one of these faults is active: which instruction or bubble
is in memory and write-back as the next one enters execute, and the two
already fetched behind it.

| Mutant | RTL edit | What the model predicts |
|---|---|---|
| `no_mem_forwarding` | The memory-stage bypass is never selected | An operand produced by the instruction just ahead is read stale |
| `no_wb_forwarding` | The write-back bypass is never selected | An operand produced two ahead (or by a load, after its stall) is read stale |
| `no_load_use_stall` | The load-use hazard is never detected | The use gets what the memory stage forwards for a load: its ALU output under the load's funct3 (the address for `lb`/`lw`, an XOR for `lbu`) |
| `no_branch_flush` | Taken jumps and branches flush nothing | The two instructions behind a taken one run before the target |

Five of the classic mutants are ported too; `regfile_wrong_rd` is not,
because the bypass hands the right value to the next two instructions,
which the model's version of the fault does not model.

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
crosslayer suite experiments/rv32i-pipeline/suite.yaml --out runs -j auto   # pipelined core
```

Compiled simulations are cached by a SHA-256 of everything `iverilog`
compiles (source text after mutants, defines, flags) and the identity of the
`iverilog` and `vvp` binaries, so runs that share RTL skip compilation; the
manifest's `build.compile_cache` records the key and whether it hit. The
cache lives in `%LOCALAPPDATA%/crosslayer/rv32i-sim` (Windows) or
`~/.cache/crosslayer/rv32i-sim`; set `CROSSLAYER_CACHE_DIR` to move it, or to
`off` to disable it.

The baseline must pass every contract that applies: all 14 on the
pipelined core; on PicoRV32 and Quark the two hazard contracts are SKIPPED.
Each mutant experiment declares
`expect: fail` and depends on the baseline, so a broken clean run blocks mutant
claims instead of producing misleading fault-detection results.

Every run bundle contains `report.html`. Open it locally and move through the
five numbered steps:

1. Confirm the overall verdict and run scope.
2. Check the adapter, system hash, seed, configuration, and injected fault.
3. Follow build, execute, collect, normalize, evaluate, and write timings.
4. Search or filter the contract results.
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
reference model's fields at the same retirement order (`rv.ref_*`), the
decoded operation, the assembly source line, cycles per instruction (timed
formats), and a raw `trace.log#L…` reference. It also carries:

* `rv.exp_*`: the result, next PC, and data access the reference semantics
  give for the operand values this instruction actually read
  (`iss.local_effects`). With operand values, these check each unit against
  its own inputs; without them they equal `rv.ref_*`.
* `rv.rs1_dist`, `rv.rs2_dist`: how many retirements back each operand was
  produced (0: no producer yet), and `rv.rs1_fwd`, `rv.rs2_fwd`: 1 if that
  is within the core's bypass window, so the value may have come by
  forwarding.

The `cpu.boot` event records the trace format, whether it carries cycles
(`rv.timed`), operand values (`rv.has_operands`), and its own next PCs
(`rv.has_next_pc`), and whether the core is pipelined (`rv.pipelined`,
`rv.bypass_window`).

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
`lockstep_operands` and `forwarded_operands` need operand values,
`cpi_budget` needs cycles, and `redirect_followed` needs next PCs the trace
recorded rather than inferred. A commit log takes each next PC from the
following line, so on the pipelined core a wrong-path instruction shows at
the branch before it (`iss.with_inferred_next_pc` predicts this). The
same mutants are still caught; one is caught later. `regfile_wrong_rd` writes
the wrong register, which RVFI shows at the next read of it, but a commit log
only shows it once a result depends on that value. The tests predict each
format's first failing instruction from the model, compared on the fields
that format observes. A timing-only fault (`mem_wait_states`) is invisible
without cycles, and caught through a waveform (`vcd_mem_wait_states`).

## Contracts

| Contract | Boundary | Check |
|---|---|---|
| `rv32i.sequential_pc` | fetch → decoder | Non-control-flow instructions advance to `PC + 4` |
| `rv32i.jump_target` | decoder → fetch | Jumps and branches select a decoded legal target |
| `rv32i.reference_path` | core → tb | PC, instruction, and trap match the model: the core is on the reference path |
| `rv32i.lockstep_control_flow` | alu → fetch | The next PC is where the operands read send it |
| `rv32i.lockstep_operands` | regfile → alu | Register operands match the model (same instruction only; on a pipelined core, operands outside the bypass window) |
| `rv32i.forwarded_operands` | hazard → alu | Operands produced within the bypass window match the model (pipelined cores) |
| `rv32i.redirect_followed` | hazard → fetch | After a jump or branch, the next instruction retired is at its next PC (pipelined cores) |
| `rv32i.lockstep_writeback` | alu → regfile | Non-load results are what the operands read call for |
| `rv32i.load_data` | lsu → regfile | Loaded values match the model |
| `rv32i.lockstep_memory` | lsu → memory | Address, byte lanes, and write data are what the operands read call for |
| `rv32i.program_output` | lsu → mmio | Memory-mapped output values match in order |
| `rv32i.clean_halt` | The run reaches EBREAK at the expected retirement count |
| `rv32i.no_bus_errors` | No access leaves mapped memory |
| `rv32i.cpi_budget` | The 95th-percentile CPI of non-shift instructions is at most eight cycles (both cores shift in steps, so shift latency grows with the amount) |

### Local and end-to-end checks

Only two contracts compare the core end to end with the reference run:
`reference_path` (is the core still on the model's path?) and
`program_output`. The rest are local: each unit is checked against its own
inputs, so a failure names the unit that went wrong rather than every unit
downstream of it. An ALU handed a stale operand computes consistently with
it, so `lockstep_writeback` passes and `forwarded_operands` fails; a branch
steered off the path by a wrong register value goes where its operands say,
so `lockstep_control_flow` passes and `lockstep_operands` fails. Operands
are compared with the model only for the same instruction: once the path
has diverged, `reference_path` has said so. Loads are the exception, since
their value comes from memory and is compared with the model's run. Without
operand values (a commit log), the local checks fall back to the model's
run. [Fault localization](fault-localization.md#rv32i-contracts) measures
what this changes.

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
| `mem_wait_states` | Testbench memory timing (clock gating on the pipelined core) | CPI budget failure with matching functional results |
| `no_mem_forwarding`, `no_wb_forwarding` | Pipeline bypass mux | `forwarded_operands`: a stale operand |
| `no_load_use_stall` | Load-use hazard detection | `forwarded_operands`: the load's ALU output instead of its data |
| `no_branch_flush` | Flush on a taken jump or branch | `redirect_followed`: a wrong-path instruction retires |

Functional faults are modeled independently in `iss.py`. Simulator-enabled
tests assert that the RTL trace first differs from the clean reference at the
same retirement order predicted by that model.

## Core profiles

Each core is a directory with a `core.yaml`, its RTL, and a testbench. The
built-in profile is [`cores/picorv32/core.yaml`](../crosslayer/integrations/rv32i/cores/picorv32/core.yaml).
Select a core with `parameters.core`: a built-in name, or a path (relative to
the experiment) to a `core.yaml` or its directory. A new core or mutant needs
no Python changes. Each `core.yaml` starts with a `yaml-language-server` comment
pointing at [`schemas/rv32i-core-profile.schema.json`](../schemas/rv32i-core-profile.schema.json)
(`crosslayer schema rv32i-core-profile`), so editors validate and complete it;
the loader stays authoritative (it also checks files, mutant targets and edits).

| Key | Meaning |
|---|---|
| `name`, `description` | Lowercase identifier; free text |
| `isa`, `trace` | `rv32i`; a [trace format](#trace-sources) or a list of them (first is the default) |
| `testbench`, `sources` | Paths inside the profile directory, compiled in this order |
| `includes` | Files the sources `` `include ``: copied next to them, never compiled alone |
| `top` | Root module (`iverilog -s`), for sources that define more than one |
| `memory_images` | File name to depth in words: the program image is written under each name, zero-padded, for RTL that loads its own memory with `$readmemh` |
| `pipeline` | `stages` and `bypass_window` (how many previous results can reach execute by forwarding); enables the hazard contracts and the `hazard` subsystem |
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
| `random_mix` | `uniform` | `hazards`: operands favor the last three results, plus load-use, compute-then-branch, compute-then-store, and dependent-chain patterns |
| `nop_spacing` | `0` | Assemble with this many NOPs after every instruction (0-8), so no two instructions are within a pipeline's hazard window; used by the `space_instructions` [follow-up](guided-experiments.md) |

Random programs use the experiment seed and initialize every working
register. They mix ALU and shift instructions, LUI and AUIPC, loads and
stores of every width at every aligned byte offset of a scratch region, and
forward-only branches, JAL, and AUIPC+JALR pairs, so they always terminate.
A quarter of the branches compare a register with itself, so each branch is
taken and not taken across seeds. All working registers are XOR-folded into
the one output value; EBREAK ends the program. The `hazards` mix keeps all
of that and makes nearby instructions depend on each other; the `uniform`
mix is unchanged, so existing seeds keep their programs.

## Coverage

Every run reports ISA coverage bins in `normalization.json` and `report.md`:

| Group | Bins |
|---|---|
| `mnemonic` | Each of the 39 RV32I mnemonics the model executes, retired |
| `branch` | Each branch mnemonic, taken and not taken |
| `memory` | Each load and store width at each aligned byte offset (load offsets need operand values, so they are omitted for `commit-log`) |
| `hazard` | Dependences in retirement order: read after write at distance 1-3, load-use at 1-2, a branch or JALR on a value from 1-2 back, a store of the value just computed |

Every suite runs `random_programs` as a 20-seed sweep, and the suite report
lists bins no seed hit. On every core the only one is `ecall`, which ends a
run like EBREAK and so is never generated. The pipeline suite adds a
`hazard_programs` sweep and a 10-seed hazard sweep per hazard mutant, each
of which must catch it on every seed. Over the same 20 seeds the hazards mix
hits load-use at distance 1 636 times against 90, a store of the value just
computed 472 times against 58, and read after write at distance 1 3,902
times against 1,130. `isa_coverage` adds
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
assembler and programs, on two multi-cycle cores and one in-order pipeline
(no branch prediction yet). It does not claim compressed instructions, multiply /
divide extensions, interrupts, caches, operating-system boot, synthesis, or
physical hardware coverage.
