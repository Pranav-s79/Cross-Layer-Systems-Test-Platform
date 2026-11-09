# RTL processor verification (RV32I)

The `rv32i` adapter runs the unmodified PicoRV32 core with Icarus Verilog,
normalizes its RISC-V Formal Interface (RVFI) retirement trace, and compares
every retired instruction with an independent RV32I instruction-set model.

The bundled RTL is pinned to YosysHQ PicoRV32 commit
`ef203c2b0a3fb793280f5114941416c425c5b461`. Its normalized Git blob matches
upstream. See `crosslayer/integrations/rv32i/rtl/README.md` for provenance and
license details.

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
```

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

The testbench provides 64 KiB of memory and prints one `RET` record for every
RVFI retirement. The adapter preserves and indexes:

| Artifact | Purpose |
|---|---|
| `trace.log` | Raw boot, retirement, output, bus-error, halt, and timeout records |
| `program.s` | Exact assembly source |
| `program.hex` | Memory image passed to `$readmemh` |
| `program.lst` | Address and machine word mapped to the source line |
| `rtl.diff` | Exact run-local RTL edit for an injected mutant |
| `process.json` | Simulator command, exit status, and duration |
| `waveform.vcd` | Optional top-level and core waveform |

Each normalized `cpu.retire` event includes the observed RVFI fields, the
reference model's fields at the same retirement order, the decoded operation,
the assembly source line, cycles per instruction, and a raw `trace.log#L…`
reference.

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
| `rv32i.cpi_budget` | The 95th-percentile CPI is at most eight cycles |

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

## Parameters

| Parameter | Default | Meaning |
|---|---:|---|
| `program` | `selftest` | Built-in name, `random`, or path to a `.s` file |
| `max_cycles` | `100000` | Testbench cycle limit |
| `waveform` | `false` | Write `waveform.vcd` when true |
| `random_instructions` | `300` | Body length for `program: random` |

Random programs use the experiment seed, initialize every working register,
stay within an aligned scratch region, avoid unbounded control flow, emit one
output value, and terminate with EBREAK.

## Scope

This phase verifies the RV32I base instruction subset exercised by the bundled
assembler and programs. It does not claim compressed instructions, multiply /
divide extensions, interrupts, caches, operating-system boot, synthesis, or
physical hardware coverage.
