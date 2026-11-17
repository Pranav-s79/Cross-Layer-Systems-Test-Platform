# FemtoRV32 Quark source provenance

`femtorv32_quark.v` is the unmodified upstream RTL from
[BrunoLevy/learn-fpga](https://github.com/BrunoLevy/learn-fpga)
(`FemtoRV/RTL/PROCESSOR/femtorv32_quark.v`) at commit
`0a97ede525c54e12f9ce5c425b0f84bc2b80bdc2`.

- Git blob: `52705e57b8eedf37387a32284eb4996116610b1c` (matches upstream)
- License: BSD-3-Clause, reproduced in `LICENSE`
- Local modifications: none; fault injection edits a run-local copy

`tb.v` is the CrossLayer testbench. Quark has no RISC-V Formal Interface, so
a retirement monitor in the testbench reads the core's internal state through
hierarchical references and records each retired instruction in RVFI terms.
The testbench overrides two parameters: `ADDR_WIDTH = 32` (the default 24 bits
cannot reach the output port at `0x1000_0000`) and `RESET_ADDR = 0`.
