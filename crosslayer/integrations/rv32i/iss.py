"""Reference RV32I instruction-set model: the golden model for lockstep checks.

Runs a program image one instruction at a time and records what each retired
instruction did, in the terms of the RISC-V Formal Interface (RVFI) that the
RTL testbench prints: PC, instruction, registers read and written, next PC,
and the data access as seen on the core's 32-bit bus (word-aligned address,
byte-lane write mask, write data in its lanes).

``faults`` applies the same bugs as the RTL mutants, modeled independently
here. The first record where a faulty run differs from a clean run is the
ground-truth earliest divergence that the RTL contracts should report.

Pipeline faults (``PIPELINE_FAULTS``) are bugs in the hazard logic of a
classic in-order five-stage pipeline (fetch, decode, execute, memory,
write-back) that forwards results to execute from memory and write-back,
stalls one cycle when an instruction uses the result of the load just ahead
of it, and resolves jumps and branches in execute, flushing the two younger
instructions behind a taken one. A clean pipeline is invisible to software,
so the model only tracks pipeline state when one of these faults is active:
which instructions (or bubbles) are in memory and write-back when the next
one enters execute, and which two have already been fetched behind it.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field, replace
from typing import Sequence

MEM_BYTES = 64 * 1024
OUT_ADDR = 0x1000_0000
"""Stores here are program output (a memory-mapped port), not memory."""
EBREAK = 0x0010_0073
MASK32 = 0xFFFF_FFFF

PIPELINE_FAULTS = frozenset({"no_mem_forwarding", "no_wb_forwarding", "no_load_use_stall", "no_branch_flush"})
"""Execute does not receive results from memory / from write-back; a load
followed by a use of its result does not stall (execute gets the value the
memory stage holds for a load: its ALU result, not the loaded data); taken
jumps and branches do not flush the two instructions fetched behind them."""
MODEL_FAULTS = frozenset(
    {"bne_inverted", "sub_as_add", "slt_unsigned", "jal_imm_bit11", "regfile_wrong_rd", "store_byte_lane",
     *PIPELINE_FAULTS}
)


def _sext(value: int, bits: int) -> int:
    sign = 1 << (bits - 1)
    return (value & (sign - 1)) - (value & sign)


def _signed(value: int) -> int:
    return value - (1 << 32) if value & 0x8000_0000 else value


def lanes(wmask: int) -> int:
    """Byte-lane mask (0b0101) -> bit mask (0x00FF00FF)."""
    return sum(0xFF << (8 * i) for i in range(4) if wmask >> i & 1)


@dataclass(frozen=True)
class Decoded:
    mnemonic: str
    kind: str
    """alu, upper, load, store, branch, jal, jalr, system, or illegal."""
    rd: int
    rs1: int
    rs2: int
    imm: int
    uses_rs1: bool
    uses_rs2: bool


_R = {(0x00, 0): "add", (0x20, 0): "sub", (0x00, 1): "sll", (0x00, 2): "slt", (0x00, 3): "sltu",
      (0x00, 4): "xor", (0x00, 5): "srl", (0x20, 5): "sra", (0x00, 6): "or", (0x00, 7): "and"}
_I = {0: "addi", 2: "slti", 3: "sltiu", 4: "xori", 6: "ori", 7: "andi"}
_LOADS = {0: "lb", 1: "lh", 2: "lw", 4: "lbu", 5: "lhu"}
_STORES = {0: "sb", 1: "sh", 2: "sw"}
_BRANCHES = {0: "beq", 1: "bne", 4: "blt", 5: "bge", 6: "bltu", 7: "bgeu"}
_SHIFTS_I = ("slli", "srli", "srai")
MNEMONICS = (
    *_R.values(), *_I.values(), *_SHIFTS_I, *_LOADS.values(), *_STORES.values(), *_BRANCHES.values(),
    "lui", "auipc", "jal", "jalr", "ecall", "ebreak",
)
"""Every RV32I mnemonic the model executes (FENCE is not modeled)."""
BRANCH_MNEMONICS = tuple(_BRANCHES.values())
ACCESS_BYTES = {"lb": 1, "lbu": 1, "lh": 2, "lhu": 2, "lw": 4, "sb": 1, "sh": 2, "sw": 4}


def decode(insn: int) -> Decoded:
    opcode, rd, f3 = insn & 0x7F, (insn >> 7) & 0x1F, (insn >> 12) & 7
    rs1, rs2, f7 = (insn >> 15) & 0x1F, (insn >> 20) & 0x1F, insn >> 25
    imm_i = _sext(insn >> 20, 12)
    illegal = Decoded("illegal", "illegal", 0, 0, 0, 0, False, False)

    if opcode == 0x33 and (f7, f3) in _R:
        return Decoded(_R[(f7, f3)], "alu", rd, rs1, rs2, 0, True, True)
    if opcode == 0x13:
        if f3 in _I:
            return Decoded(_I[f3], "alu", rd, rs1, 0, imm_i, True, False)
        shifts = {(0x00, 1): "slli", (0x00, 5): "srli", (0x20, 5): "srai"}
        if (f7, f3) in shifts:
            return Decoded(shifts[(f7, f3)], "alu", rd, rs1, 0, rs2, True, False)
        return illegal
    if opcode == 0x03 and f3 in _LOADS:
        return Decoded(_LOADS[f3], "load", rd, rs1, 0, imm_i, True, False)
    if opcode == 0x23 and f3 in _STORES:
        imm = _sext(((insn >> 25) << 5) | ((insn >> 7) & 0x1F), 12)
        return Decoded(_STORES[f3], "store", 0, rs1, rs2, imm, True, True)
    if opcode == 0x63 and f3 in _BRANCHES:
        imm = _sext(
            ((insn >> 31) & 1) << 12 | ((insn >> 7) & 1) << 11 | ((insn >> 25) & 0x3F) << 5 | ((insn >> 8) & 0xF) << 1,
            13,
        )
        return Decoded(_BRANCHES[f3], "branch", 0, rs1, rs2, imm, True, True)
    if opcode in (0x37, 0x17):
        return Decoded("lui" if opcode == 0x37 else "auipc", "upper", rd, 0, 0, _sext(insn & 0xFFFFF000, 32), False, False)
    if opcode == 0x6F:
        imm = _sext(
            ((insn >> 31) & 1) << 20 | ((insn >> 12) & 0xFF) << 12 | ((insn >> 20) & 1) << 11 | ((insn >> 21) & 0x3FF) << 1,
            21,
        )
        return Decoded("jal", "jal", rd, 0, 0, imm, False, False)
    if opcode == 0x67 and f3 == 0:
        return Decoded("jalr", "jalr", rd, rs1, 0, imm_i, True, False)
    if insn == EBREAK:
        return Decoded("ebreak", "system", 0, 0, 0, 0, False, False)
    if insn == 0x0000_0073:
        return Decoded("ecall", "system", 0, 0, 0, 0, False, False)
    return illegal


@dataclass(frozen=True)
class Retire:
    """One retired instruction, as RVFI reports it."""

    order: int
    pc: int
    insn: int
    mnemonic: str
    kind: str
    rs1: int
    rs1_val: int
    rs2: int
    rs2_val: int
    rd: int
    rd_val: int
    next_pc: int
    mem_addr: int
    mem_wmask: int
    mem_wdata: int
    """Write data with lanes outside ``mem_wmask`` cleared."""
    trap: bool


@dataclass(frozen=True)
class _Slot:
    """An instruction in the memory or write-back stage, for operand forwarding."""

    rd: int
    """Register it writes (0: none)."""
    old: int
    """That register's value before the write (what the register file still holds)."""
    forwarded: int
    """What the memory stage forwards: the result, or for a load its ALU output."""
    value: int
    """What write-back forwards and writes: the result, or for a load the loaded data."""
    rd_field: int
    """The raw rd field, which the load-use check compares (even when it is x0)."""
    load: bool


@dataclass
class ModelRun:
    retires: list[Retire]
    outputs: list[int]
    halt: str
    """ebreak, ecall, illegal, misaligned, or limit."""
    bus_errors: list[int] = field(default_factory=list)


class Model:
    def __init__(self, image: bytes, *, faults: frozenset[str] | set[str] = frozenset()) -> None:
        unknown = set(faults) - MODEL_FAULTS
        if unknown:
            raise ValueError(f"unknown model faults {sorted(unknown)}")
        if len(image) > MEM_BYTES:
            raise ValueError(f"program image of {len(image)} bytes exceeds {MEM_BYTES} bytes of memory")
        self.faults = frozenset(faults)
        self.pipelined = bool(self.faults & PIPELINE_FAULTS)
        self._slots: deque[_Slot | None] = deque([None, None], maxlen=2)
        """Write-back and memory stages (oldest first) as the next instruction enters execute."""
        self._fetched: deque[int] = deque([4, 8])
        """PCs already fetched behind the instruction in execute (decode, fetch)."""
        self.mem = bytearray(MEM_BYTES)
        self.mem[: len(image)] = image
        self.regs = [0] * 32
        self.pc = 0
        self.outputs: list[int] = []
        self.bus_errors: list[int] = []

    # -- memory, as seen on the 32-bit bus ----------------------------------------------

    def _read_word(self, addr: int) -> int:
        if addr == OUT_ADDR:
            return 0
        if addr + 4 > MEM_BYTES:
            self.bus_errors.append(addr)
            return 0
        return int.from_bytes(self.mem[addr : addr + 4], "little")

    def _write_word(self, addr: int, data: int, wmask: int) -> None:
        if addr == OUT_ADDR:
            self.outputs.append(data & lanes(wmask))
            return
        if addr + 4 > MEM_BYTES:
            self.bus_errors.append(addr)
            return
        for i in range(4):
            if wmask >> i & 1:
                self.mem[addr + i] = (data >> (8 * i)) & 0xFF

    def _set(self, rd: int, value: int) -> None:
        if "regfile_wrong_rd" in self.faults:
            rd ^= 1
        if rd:
            self.regs[rd] = value & MASK32

    # -- execution ----------------------------------------------------------------------

    def run(self, max_steps: int = 100_000) -> ModelRun:
        retires: list[Retire] = []
        halt = "limit"
        for order in range(max_steps):
            record, halt_reason = self.step(order)
            retires.append(record)
            if halt_reason is not None:
                halt = halt_reason
                break
        return ModelRun(retires, self.outputs, halt, self.bus_errors)

    # -- pipeline state (only with pipeline faults) -------------------------------------

    def _enter_execute(self, insn: int) -> None:
        """The load-use check in decode: a bubble enters first if the instruction
        reads (by the pipeline's own rule, x0 included) what the load ahead writes."""
        ahead = self._slots[-1]
        if ahead is None or not ahead.load or "no_load_use_stall" in self.faults:
            return
        opcode = insn & 0x7F
        reads_rs1 = opcode not in (0x6F, 0x17, 0x37)  # every opcode but JAL, AUIPC, LUI
        reads_rs2 = opcode in (0x33, 0x63, 0x23)  # register ALU ops, branches, stores
        if (reads_rs1 and (insn >> 15) & 0x1F == ahead.rd_field) or (reads_rs2 and (insn >> 20) & 0x1F == ahead.rd_field):
            self._slots.append(None)

    def _read(self, r: int) -> int:
        """Register ``r`` as execute sees it: forwarded from memory, else from
        write-back, else from the register file (without the pending writes)."""
        if not self.pipelined or r == 0:
            return self.regs[r]
        wb, mem = self._slots
        if mem is not None and mem.rd == r and "no_mem_forwarding" not in self.faults:
            return mem.forwarded
        if wb is not None and wb.rd == r and "no_wb_forwarding" not in self.faults:
            return wb.value
        if wb is not None and wb.rd == r:
            return wb.old
        if mem is not None and mem.rd == r:
            return mem.old
        return self.regs[r]

    def _leave_execute(self, insn: int, d: Decoded, a: int, rd: int, rd_val: int, old: int, taken: bool) -> None:
        """Push the instruction into memory; a taken jump or branch flushes the
        two instructions behind it (two bubbles), unless the flush is broken,
        in which case they run before the target."""
        writes = d.kind not in ("branch", "store") and rd != 0
        forwarded = rd_val
        if d.kind == "load":  # the memory stage holds the address-path ALU output for a load
            f3 = (insn >> 12) & 7
            op = {0: "addi", 1: "slli", 2: "slti", 3: "sltiu", 4: "xori", 6: "ori", 7: "andi"}.get(
                f3, "srai" if insn >> 30 & 1 else "srli")
            forwarded = self._alu(Decoded(op, "alu", rd, d.rs1, 0, d.imm, True, False), a, 0)
        self._slots.append(_Slot(rd if writes else 0, old, forwarded, rd_val, (insn >> 7) & 0x1F, d.kind == "load"))
        if taken and "no_branch_flush" not in self.faults:
            self._slots.extend((None, None))

    def step(self, order: int) -> tuple[Retire, str | None]:  # noqa: C901 - one branch per kind
        pc = self.pc
        insn = self._read_word(pc) if pc % 4 == 0 else 0
        d = decode(insn)
        if self.pipelined:
            self._enter_execute(insn)
        a = self._read(d.rs1) if d.uses_rs1 else 0
        b = self._read(d.rs2) if d.uses_rs2 else 0
        next_pc = (pc + 4) & MASK32
        taken = d.kind in ("jal", "jalr")
        rd_val = 0
        mem_addr = mem_wmask = mem_wdata = 0
        trap: str | None = None

        if d.kind == "alu":
            rd_val = self._alu(d, a, b)
        elif d.kind == "upper":
            rd_val = (d.imm if d.mnemonic == "lui" else pc + d.imm) & MASK32
        elif d.kind == "jal":
            imm = d.imm & ~0x800 if "jal_imm_bit11" in self.faults else d.imm
            rd_val, next_pc = (pc + 4) & MASK32, (pc + imm) & MASK32
        elif d.kind == "jalr":
            rd_val, next_pc = (pc + 4) & MASK32, (a + d.imm) & MASK32 & ~1
        elif d.kind == "branch":
            if self._taken(d.mnemonic, a, b):
                next_pc = (pc + d.imm) & MASK32
                taken = True
        elif d.kind == "load":
            addr = (a + d.imm) & MASK32
            size = {"lb": 1, "lbu": 1, "lh": 2, "lhu": 2, "lw": 4}[d.mnemonic]
            if addr % size:
                trap = "misaligned"
            else:
                mem_addr = addr & ~3
                word = self._read_word(mem_addr)
                raw = (word >> (8 * (addr & 3))) & ((1 << (8 * size)) - 1)
                rd_val = _sext(raw, 8 * size) & MASK32 if d.mnemonic in ("lb", "lh") else raw
        elif d.kind == "store":
            addr = (a + d.imm) & MASK32
            size = {"sb": 1, "sh": 2, "sw": 4}[d.mnemonic]
            if addr % size:
                trap = "misaligned"
            else:
                mem_addr, shift = addr & ~3, 8 * (addr & 3)
                if d.mnemonic == "sb" and "store_byte_lane" in self.faults:
                    shift = 0
                mem_wmask = ((1 << size) - 1) << (shift // 8)
                mem_wdata = ((b & ((1 << (8 * size)) - 1)) << shift) & lanes(mem_wmask)
                self._write_word(mem_addr, mem_wdata, mem_wmask)
        elif d.kind == "system":
            trap = d.mnemonic
        else:
            trap = "illegal"

        if trap is not None:
            record = Retire(order, pc, insn, d.mnemonic, d.kind, 0, 0, 0, 0, 0, 0, pc, 0, 0, 0, True)
            return record, trap

        rd = d.rd if d.kind not in ("branch", "store") else 0
        old = self.regs[rd]
        if rd:
            self._set(rd, rd_val)
        else:
            rd_val = 0
        self.pc = next_pc
        if self.pipelined:
            self._leave_execute(insn, d, a, rd, rd_val, old, taken)
            if "no_branch_flush" in self.faults:  # what was fetched behind it runs first
                self._fetched.append(next_pc if taken else (self._fetched[-1] + 4) & MASK32)
                self.pc = self._fetched.popleft()
        record = Retire(
            order, pc, insn, d.mnemonic, d.kind,
            d.rs1 if d.uses_rs1 else 0, a, d.rs2 if d.uses_rs2 else 0, b,
            rd, rd_val, next_pc, mem_addr, mem_wmask, mem_wdata, False,
        )
        return record, None

    def _signed_lt(self, a: int, b: int) -> bool:
        if "slt_unsigned" in self.faults:
            return a < b
        return _signed(a) < _signed(b)

    def _alu(self, d: Decoded, a: int, b: int) -> int:
        op, imm = d.mnemonic, d.imm & MASK32
        rhs = b if d.uses_rs2 else imm
        shamt = (b if d.uses_rs2 else d.imm) & 0x1F
        if op in ("add", "addi"):
            return (a + rhs) & MASK32
        if op == "sub":
            return (a + b if "sub_as_add" in self.faults else a - b) & MASK32
        if op in ("slt", "slti"):
            return int(self._signed_lt(a, rhs))
        if op in ("sltu", "sltiu"):
            return int(a < rhs)
        if op in ("xor", "xori"):
            return a ^ rhs
        if op in ("or", "ori"):
            return a | rhs
        if op in ("and", "andi"):
            return a & rhs
        if op in ("sll", "slli"):
            return (a << shamt) & MASK32
        if op in ("srl", "srli"):
            return a >> shamt
        if op in ("sra", "srai"):
            return (_signed(a) >> shamt) & MASK32
        raise AssertionError(op)

    def _taken(self, op: str, a: int, b: int) -> bool:
        if op == "beq":
            return a == b
        if op == "bne":
            return a == b if "bne_inverted" in self.faults else a != b
        if op == "blt":
            return self._signed_lt(a, b)
        if op == "bge":
            return not self._signed_lt(a, b)
        if op == "bltu":
            return a < b
        return a >= b  # bgeu


@dataclass(frozen=True)
class Effects:
    """What a correct core does with one instruction given the operand values it read."""

    rd: int
    rd_val: int | None
    """None for a load: the value comes from memory, not from the operands."""
    next_pc: int
    mem_addr: int
    mem_wmask: int
    mem_wdata: int


_CLEAN = Model(b"")


def local_effects(insn: int, pc: int, a: int, b: int) -> Effects | None:
    """The result, next PC, and data access of ``insn`` at ``pc`` with operands
    ``a`` and ``b``, by the reference semantics; None for traps and misaligned
    accesses.

    Comparing a core against this rather than against the reference run
    separates a unit that computes wrongly from one that was handed wrong
    inputs: an instruction fed a stale operand still computes consistently.
    """
    d = decode(insn)
    rd = d.rd if d.kind not in ("branch", "store") else 0
    next_pc = (pc + 4) & MASK32
    if d.kind == "jal":
        next_pc = (pc + d.imm) & MASK32
    elif d.kind == "jalr":
        next_pc = (a + d.imm) & MASK32 & ~1
    elif d.kind == "branch" and _CLEAN._taken(d.mnemonic, a, b):
        next_pc = (pc + d.imm) & MASK32
    if d.kind == "alu":
        value: int | None = _CLEAN._alu(d, a, b)
    elif d.kind == "upper":
        value = (d.imm if d.mnemonic == "lui" else pc + d.imm) & MASK32
    elif d.kind in ("jal", "jalr"):
        value = (pc + 4) & MASK32
    elif d.kind == "branch":
        value = 0
    elif d.kind in ("load", "store"):
        addr = (a + d.imm) & MASK32
        size = ACCESS_BYTES[d.mnemonic]
        if addr % size:
            return None
        if d.kind == "load":
            return Effects(rd, None, next_pc, addr & ~3, 0, 0)
        shift = 8 * (addr & 3)
        wmask = ((1 << size) - 1) << (shift // 8)
        return Effects(0, 0, next_pc, addr & ~3, wmask, ((b & ((1 << (8 * size)) - 1)) << shift) & lanes(wmask))
    else:
        return None
    return Effects(rd, value if rd else 0, next_pc, 0, 0, 0)


def with_inferred_next_pc(retires: Sequence[Retire]) -> list[Retire]:
    """``retires`` as a trace that takes each next PC from the following
    retirement would show them (see ``TraceSource.next_pc_inferred``)."""
    out = list(retires)
    for i in range(len(out) - 1):
        if not out[i].trap:
            out[i] = replace(out[i], next_pc=out[i + 1].pc)
    return out


def first_divergence(clean: list[Retire], faulty: list[Retire], fields: Sequence[str] | None = None) -> int | None:
    """Order of the first retired instruction that differs, or None.

    ``fields`` limits the comparison to what a trace source can observe (a
    commit log has no operand values, so a wrong register read shows only
    once it corrupts a result).
    """
    for x, y in zip(clean, faulty):
        if (x != y) if fields is None else any(getattr(x, f) != getattr(y, f) for f in fields):
            return x.order
    if len(clean) != len(faulty):
        return min(len(clean), len(faulty))
    return None
