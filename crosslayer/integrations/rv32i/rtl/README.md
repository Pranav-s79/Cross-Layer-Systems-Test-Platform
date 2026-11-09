# PicoRV32 source provenance

`picorv32.v` is the unmodified upstream RTL from
[YosysHQ/picorv32](https://github.com/YosysHQ/picorv32) at commit
`ef203c2b0a3fb793280f5114941416c425c5b461` (2026-09-07).

- Normalized Git blob: `cc45fa998f1857b06a856ea59b931f6c37e06b34`
- License: ISC, reproduced in the file header
- Local modifications: none; fault injection edits a run-local copy

`tb.v` is the CrossLayer testbench. It drives PicoRV32's native memory
interface and records the upstream RISC-V Formal Interface (RVFI) signals.
