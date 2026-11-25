# learn-fpga five-stage pipeline: source provenance

`pipeline6.v` is the unmodified upstream RTL from
[BrunoLevy/learn-fpga](https://github.com/BrunoLevy/learn-fpga)
(`FemtoRV/TUTORIALS/FROM_BLINKER_TO_RISCV/pipeline6.v`, "From Blinker to
RISC-V" episode II, step 6) at commit
`f5ada24ae6cca67d99889daec2c4ee44a6e59417`.

| File | Origin | Git blob |
|---|---|---|
| `pipeline6.v` | upstream, unmodified | `a5e58280f31de3d162b2c5416062b60ee88190c4` |
| `riscv_disassembly.v` | upstream, unmodified (included inside the core; used only with `VERBOSE`) | `7935f6c91a2b662ad3f129bb9557092d4277dbe7` |
| `emitter_uart.v` | upstream, unmodified (used only by the SOC wrapper) | `5a3a935c33eec1fc66ceb1711db3ff37632bc74c` |
| `clockworks.v` | **CrossLayer stub**, not upstream | - |
| `tb.v` | CrossLayer testbench | - |

License: BSD-3-Clause, reproduced in `LICENSE`. Fault injection edits a
run-local copy.

`pipeline6.v` contains the core (`Processor`) and a board-level `SOC`
wrapper. The upstream `clockworks.v`, which only `SOC` uses, includes
`../../RTL/PLL/femtopll.v` (FPGA PLLs) by a path outside this directory, so
it is replaced by a stub with the same module interface. `core.yaml` sets
`top: tb`, so `SOC` is never elaborated, and defines `CPU_FREQ`, which `SOC`
references.

## The core

A classic in-order RV32I pipeline: fetch, decode, execute, memory,
write-back. Execute receives operands forwarded from the memory and
write-back stages; a load followed by a use of its result stalls fetch and
decode for one cycle; jumps and branches resolve in execute, and a taken
one flushes decode and fetch. Separate 64 KiB instruction and data
memories, loaded by the core from `PROGROM.hex` and `DATARAM.hex`
(`memory_images` in `core.yaml`). It has no RVFI port and no traps; EBREAK
and ECALL halt it.

`tb.v` documents the retirement monitor: operands are captured in execute
after forwarding and carried in two shadow stages (the core never stalls
execute, memory, or write-back), and an instruction retires when it leaves
write-back unless it is the core's bubble (`add x0, x0, x0`), the rule the
core's own `instret` counter uses.

## Why this core

Milestone 3.7 needed a permissively licensed RV32I core with a real
pipeline (forwarding, stalls, flushes) that Icarus Verilog compiles as
plain Verilog. Candidates considered:

| Candidate | Why not, or why |
|---|---|
| **learn-fpga `pipeline6.v`** (chosen) | BSD-3-Clause, Verilog-2005, about 600 lines, simulated upstream with Icarus. Full forwarding, a load-use stall, and a two-slot branch flush, each a few readable lines, so each hazard mutant is a one- or two-line edit. Same upstream as Quark. |
| learn-fpga `pipeline7.v`-`pipeline9.v` | Add static and dynamic branch prediction and a return-address stack. More to verify, but prediction changes timing only; a later milestone. |
| UltraEmbedded biRISC-V | A larger dual-issue RV32IM core with CSRs and privilege modes; much more than RV32I lockstep needs today. |
| VexRiscv | Verilog generated from SpinalHDL: correct and configurable, but mutants of generated code are hard to read or keep stable. |
| Ibex, CVA5 | SystemVerilog cores, simulated upstream with Verilator or commercial tools; Icarus's SystemVerilog support does not cover them. |
| PicoRV32, FemtoRV32 Quark | Already in the suite; both multi-cycle, with no hazards to get wrong. |

## Mutants

Four hazard-logic mutants (`no_mem_forwarding`, `no_wb_forwarding`,
`no_load_use_stall`, `no_branch_flush`) and five of the six classic ones.
`regfile_wrong_rd` is not ported: the bypass hands the right value to the
next two instructions, so the reference model's fault, which assumes no
bypass, would predict the divergence too early.
