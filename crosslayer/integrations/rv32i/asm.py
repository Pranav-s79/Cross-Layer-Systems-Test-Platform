"""A small two-pass RV32I assembler for test programs.

Enough to write programs for the reference core without a cross-compiler:
the RV32I base instructions, common pseudo-instructions, labels, and the
directives ``.word``, ``.space``, ``.align``, ``.equ``. Comments start with
``#``. Numeric local labels work as in the GNU assembler: ``1:`` defines one,
``1f`` / ``1b`` refer to the next / previous ``1:``. Every instruction is 4
bytes; the image starts at address 0.

Each emitted word remembers its source line, so a program counter in a trace
can be mapped back to the line of assembly that produced it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_ABI = (
    "zero ra sp gp tp t0 t1 t2 s0 s1 a0 a1 a2 a3 a4 a5 a6 a7 "
    "s2 s3 s4 s5 s6 s7 s8 s9 s10 s11 t3 t4 t5 t6"
).split()
REGISTERS: dict[str, int] = {name: i for i, name in enumerate(_ABI)}
REGISTERS.update({f"x{i}": i for i in range(32)})
REGISTERS["fp"] = 8

_R = {  # name -> (funct7, funct3)
    "add": (0x00, 0), "sub": (0x20, 0), "sll": (0x00, 1), "slt": (0x00, 2),
    "sltu": (0x00, 3), "xor": (0x00, 4), "srl": (0x00, 5), "sra": (0x20, 5),
    "or": (0x00, 6), "and": (0x00, 7),
}
_I_ALU = {"addi": 0, "slti": 2, "sltiu": 3, "xori": 4, "ori": 6, "andi": 7}
_SHIFT_I = {"slli": (0x00, 1), "srli": (0x00, 5), "srai": (0x20, 5)}
_LOAD = {"lb": 0, "lh": 1, "lw": 2, "lbu": 4, "lhu": 5}
_STORE = {"sb": 0, "sh": 1, "sw": 2}
_BRANCH = {"beq": 0, "bne": 1, "blt": 4, "bge": 5, "bltu": 6, "bgeu": 7}
_BRANCH_ZERO = {"beqz": ("beq", False), "bnez": ("bne", False), "bltz": ("blt", False),
                "bgez": ("bge", False), "blez": ("bge", True), "bgtz": ("blt", True)}
_BRANCH_SWAP = {"bgt": "blt", "ble": "bge", "bgtu": "bltu", "bleu": "bgeu"}

_MEM_OPERAND = re.compile(r"^(?P<imm>[^()]*)\((?P<reg>[^()]+)\)$")
_LABEL = re.compile(r"^[A-Za-z_.][A-Za-z0-9_.]*$")
_LOCAL_DEF = re.compile(r"^\d+$")
_LOCAL_REF = re.compile(r"^(?P<n>\d+)(?P<dir>[fb])$")
_Locals = dict[str, list[tuple[int, int]]]  # "1" -> [(statement index, address), ...]


class AsmError(ValueError):
    def __init__(self, message: str, line: int | None = None) -> None:
        super().__init__(f"line {line}: {message}" if line is not None else message)
        self.line = line


@dataclass
class Program:
    words: list[int]
    labels: dict[str, int]
    source_lines: dict[int, int] = field(default_factory=dict)
    """Byte address -> 1-based source line that produced the word."""

    def image(self) -> bytes:
        return b"".join(w.to_bytes(4, "little") for w in self.words)

    def hex_lines(self) -> str:
        """One 32-bit word per line, the format Verilog ``$readmemh`` reads."""
        return "".join(f"{w:08x}\n" for w in self.words)


def assemble(source: str) -> Program:
    statements = _parse(source)
    labels, equs, local_labels = _layout(statements)
    words: list[int] = []
    source_lines: dict[int, int] = {}
    for index, (lineno, op, args, addr) in enumerate(statements):
        if op is None:
            continue
        ctx = _Ctx(lineno, addr, labels, equs, local_labels, index)
        try:
            emitted = _emit(ctx, op, args)
        except AsmError:
            raise
        except (ValueError, KeyError, IndexError) as exc:
            raise AsmError(f"{op}: {exc}", lineno) from None
        for i, word in enumerate(emitted):
            source_lines[addr + 4 * i] = lineno
        words.extend(emitted)
    return Program(words, labels, source_lines)


# -- pass 1: parse and lay out -------------------------------------------------------


def _parse(source: str) -> list[tuple[int, str | None, list[str], int]]:
    out: list[tuple[int, str | None, list[str], int]] = []
    for lineno, raw in enumerate(source.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        while ":" in line:
            label, rest = line.split(":", 1)
            label = label.strip()
            if not (_LABEL.match(label) or _LOCAL_DEF.match(label)):
                raise AsmError(f"bad label {label!r}", lineno)
            out.append((lineno, None, [label], 0))
            line = rest.strip()
        if not line:
            continue
        op, *tail = line.split(None, 1)
        rest = tail[0] if tail else ""
        args = [a.strip() for a in rest.split(",")] if rest.strip() else []
        out.append((lineno, op.lower(), args, 0))
    return out


def _layout(
    statements: list[tuple[int, str | None, list[str], int]],
) -> tuple[dict[str, int], dict[str, int], _Locals]:
    labels: dict[str, int] = {}
    equs: dict[str, int] = {}
    local_labels: _Locals = {}
    addr = 0
    for i, (lineno, op, args, _) in enumerate(statements):
        statements[i] = (lineno, op, args, addr)
        if op is None:
            name = args[0]
            if _LOCAL_DEF.match(name):
                local_labels.setdefault(name, []).append((i, addr))
                continue
            if name in labels or name in equs:
                raise AsmError(f"duplicate label {name!r}", lineno)
            labels[name] = addr
            continue
        if op == ".equ":
            if len(args) != 2 or not _LABEL.match(args[0]):
                raise AsmError(".equ expects NAME, VALUE", lineno)
            equs[args[0]] = _int(args[1], equs, lineno)
            continue
        addr += _size(op, args, equs, addr, lineno)
    return labels, equs, local_labels


def _size(op: str, args: list[str], equs: dict[str, int], addr: int, lineno: int) -> int:
    if op in (".text", ".data", ".globl", ".equ"):
        return 0
    if op == ".word":
        return 4 * len(args)
    if op == ".space":
        n = _int(args[0], equs, lineno)
        if n < 0 or n % 4:
            raise AsmError(".space must be a non-negative multiple of 4", lineno)
        return n
    if op == ".align":
        align = 1 << _int(args[0], equs, lineno)
        return (-addr) % align
    if op == "li":
        return 4 * len(_li_parts(_int(args[1], equs, lineno)))
    if op == "la":
        return 8
    return 4


def _int(text: str, equs: dict[str, int], lineno: int) -> int:
    text = text.strip()
    if text in equs:
        return equs[text]
    try:
        return int(text, 0)
    except ValueError:
        raise AsmError(f"expected a number, got {text!r}", lineno) from None


# -- pass 2: encode ------------------------------------------------------------------


@dataclass
class _Ctx:
    lineno: int
    addr: int
    labels: dict[str, int]
    equs: dict[str, int]
    local_labels: _Locals
    index: int
    """Position of this statement, for resolving ``1f`` / ``1b``."""

    def label(self, text: str) -> int | None:
        if text in self.labels:
            return self.labels[text]
        match = _LOCAL_REF.match(text)
        if match is None:
            return None
        defs = self.local_labels.get(match["n"], [])
        if match["dir"] == "f":
            found = [addr for i, addr in defs if i > self.index]
            pick = found[0] if found else None
        else:
            found = [addr for i, addr in defs if i < self.index]
            pick = found[-1] if found else None
        if pick is None:
            raise AsmError(f"no local label for {text!r}", self.lineno)
        return pick

    def reg(self, text: str) -> int:
        name = text.strip().lower()
        if name not in REGISTERS:
            raise AsmError(f"unknown register {text!r}", self.lineno)
        return REGISTERS[name]

    def imm(self, text: str) -> int:
        return _int(text, self.equs, self.lineno)

    def value(self, text: str) -> int:
        text = text.strip()
        address = self.label(text)
        return address if address is not None else self.imm(text)

    def offset(self, text: str) -> int:
        """Branch/jump target: a label (PC-relative) or a numeric offset."""
        text = text.strip()
        address = self.label(text)
        if address is not None:
            return address - self.addr
        if _LABEL.match(text) and text not in self.equs:
            raise AsmError(f"unknown label {text!r}", self.lineno)
        return self.imm(text)

    def mem(self, text: str) -> tuple[int, int]:
        match = _MEM_OPERAND.match(text.replace(" ", ""))
        if match is None:
            raise AsmError(f"expected imm(reg), got {text!r}", self.lineno)
        imm = self.imm(match["imm"]) if match["imm"] else 0
        return imm, self.reg(match["reg"])

    def check(self, value: int, lo: int, hi: int, what: str) -> int:
        if not lo <= value <= hi:
            raise AsmError(f"{what} {value} out of range [{lo}, {hi}]", self.lineno)
        return value


def _r(f7: int, rs2: int, rs1: int, f3: int, rd: int, opcode: int) -> int:
    return (f7 << 25) | (rs2 << 20) | (rs1 << 15) | (f3 << 12) | (rd << 7) | opcode


def _i(imm: int, rs1: int, f3: int, rd: int, opcode: int) -> int:
    return ((imm & 0xFFF) << 20) | (rs1 << 15) | (f3 << 12) | (rd << 7) | opcode


def _s(imm: int, rs2: int, rs1: int, f3: int) -> int:
    imm &= 0xFFF
    return ((imm >> 5) << 25) | (rs2 << 20) | (rs1 << 15) | (f3 << 12) | ((imm & 0x1F) << 7) | 0x23


def _b(imm: int, rs2: int, rs1: int, f3: int) -> int:
    imm &= 0x1FFF
    return (
        ((imm >> 12) & 1) << 31 | ((imm >> 5) & 0x3F) << 25 | (rs2 << 20) | (rs1 << 15)
        | (f3 << 12) | ((imm >> 1) & 0xF) << 8 | ((imm >> 11) & 1) << 7 | 0x63
    )


def _u(imm20: int, rd: int, opcode: int) -> int:
    return ((imm20 & 0xFFFFF) << 12) | (rd << 7) | opcode


def _j(imm: int, rd: int) -> int:
    imm &= 0x1FFFFF
    return (
        ((imm >> 20) & 1) << 31 | ((imm >> 1) & 0x3FF) << 21 | ((imm >> 11) & 1) << 20
        | ((imm >> 12) & 0xFF) << 12 | (rd << 7) | 0x6F
    )


def _li_parts(value: int) -> list[tuple[str, int]]:
    """Split a 32-bit constant into lui/addi parts (one or two instructions)."""
    if not -(1 << 31) <= value < (1 << 32):
        raise ValueError(f"li value {value} does not fit in 32 bits")
    value &= 0xFFFFFFFF
    signed = value - (1 << 32) if value & 0x80000000 else value
    if -2048 <= signed < 2048:
        return [("addi", signed)]
    lo = ((value & 0xFFF) ^ 0x800) - 0x800  # sign-extended low 12 bits
    hi = ((value - lo) >> 12) & 0xFFFFF
    return [("lui", hi)] + ([("addi", lo)] if lo else [])


def _branch(ctx: _Ctx, name: str, rs1: int, rs2: int, target: str) -> int:
    off = ctx.offset(target)
    ctx.check(off, -4096, 4094, "branch offset")
    if off % 2:
        raise AsmError("branch offset must be even", ctx.lineno)
    return _b(off, rs2, rs1, _BRANCH[name])


def _jal(ctx: _Ctx, rd: int, target: str) -> int:
    off = ctx.offset(target)
    ctx.check(off, -(1 << 20), (1 << 20) - 2, "jump offset")
    if off % 2:
        raise AsmError("jump offset must be even", ctx.lineno)
    return _j(off, rd)


def _nargs(ctx: _Ctx, op: str, args: list[str], *counts: int) -> None:
    if len(args) not in counts:
        want = " or ".join(str(c) for c in counts)
        raise AsmError(f"{op} expects {want} operands, got {len(args)}", ctx.lineno)


def _emit(ctx: _Ctx, op: str, a: list[str]) -> list[int]:  # noqa: C901 - one branch per mnemonic
    if op in (".text", ".data", ".globl", ".equ"):
        return []
    if op == ".word":
        return [ctx.value(x) & 0xFFFFFFFF for x in a]
    if op == ".space":
        return [0] * (ctx.imm(a[0]) // 4)
    if op == ".align":
        return [0] * (((-ctx.addr) % (1 << ctx.imm(a[0]))) // 4)

    if op in _R:
        _nargs(ctx, op, a, 3)
        f7, f3 = _R[op]
        return [_r(f7, ctx.reg(a[2]), ctx.reg(a[1]), f3, ctx.reg(a[0]), 0x33)]
    if op in _I_ALU:
        _nargs(ctx, op, a, 3)
        imm = ctx.check(ctx.imm(a[2]), -2048, 2047, "immediate")
        return [_i(imm, ctx.reg(a[1]), _I_ALU[op], ctx.reg(a[0]), 0x13)]
    if op in _SHIFT_I:
        _nargs(ctx, op, a, 3)
        f7, f3 = _SHIFT_I[op]
        shamt = ctx.check(ctx.imm(a[2]), 0, 31, "shift amount")
        return [_r(f7, shamt, ctx.reg(a[1]), f3, ctx.reg(a[0]), 0x13)]
    if op in _LOAD:
        _nargs(ctx, op, a, 2)
        imm, rs1 = ctx.mem(a[1])
        return [_i(ctx.check(imm, -2048, 2047, "offset"), rs1, _LOAD[op], ctx.reg(a[0]), 0x03)]
    if op in _STORE:
        _nargs(ctx, op, a, 2)
        imm, rs1 = ctx.mem(a[1])
        return [_s(ctx.check(imm, -2048, 2047, "offset"), ctx.reg(a[0]), rs1, _STORE[op])]
    if op in _BRANCH:
        _nargs(ctx, op, a, 3)
        return [_branch(ctx, op, ctx.reg(a[0]), ctx.reg(a[1]), a[2])]
    if op in _BRANCH_ZERO:
        _nargs(ctx, op, a, 2)
        base, swap = _BRANCH_ZERO[op]
        rs = ctx.reg(a[0])
        return [_branch(ctx, base, 0, rs, a[1]) if swap else _branch(ctx, base, rs, 0, a[1])]
    if op in _BRANCH_SWAP:
        _nargs(ctx, op, a, 3)
        return [_branch(ctx, _BRANCH_SWAP[op], ctx.reg(a[1]), ctx.reg(a[0]), a[2])]
    if op in ("lui", "auipc"):
        _nargs(ctx, op, a, 2)
        imm = ctx.check(ctx.imm(a[1]), -(1 << 19), (1 << 20) - 1, "upper immediate")
        return [_u(imm, ctx.reg(a[0]), 0x37 if op == "lui" else 0x17)]
    if op == "jal":
        _nargs(ctx, op, a, 1, 2)
        rd, target = (1, a[0]) if len(a) == 1 else (ctx.reg(a[0]), a[1])
        return [_jal(ctx, rd, target)]
    if op == "jalr":
        _nargs(ctx, op, a, 1, 2, 3)
        if len(a) == 1:
            rd, imm, rs1 = 1, 0, ctx.reg(a[0])
        elif len(a) == 2:
            rd, (imm, rs1) = ctx.reg(a[0]), ctx.mem(a[1])
        else:
            rd, rs1, imm = ctx.reg(a[0]), ctx.reg(a[1]), ctx.imm(a[2])
        return [_i(ctx.check(imm, -2048, 2047, "offset"), rs1, 0, rd, 0x67)]
    if op == "ecall":
        return [0x00000073]
    if op == "ebreak":
        return [0x00100073]
    if op == "fence":
        return [0x0FF0000F]

    # pseudo-instructions
    if op == "nop":
        return [_i(0, 0, 0, 0, 0x13)]
    if op == "li":
        _nargs(ctx, op, a, 2)
        rd = ctx.reg(a[0])
        out = []
        for name, imm in _li_parts(ctx.imm(a[1])):
            out.append(_u(imm, rd, 0x37) if name == "lui" else _i(imm, rd if out else 0, 0, rd, 0x13))
        return out
    if op == "la":
        _nargs(ctx, op, a, 2)
        rd = ctx.reg(a[0])
        off = ctx.value(a[1]) - ctx.addr
        lo = ((off & 0xFFF) ^ 0x800) - 0x800
        return [_u((off - lo) >> 12, rd, 0x17), _i(lo, rd, 0, rd, 0x13)]
    if op == "mv":
        _nargs(ctx, op, a, 2)
        return [_i(0, ctx.reg(a[1]), 0, ctx.reg(a[0]), 0x13)]
    if op == "not":
        _nargs(ctx, op, a, 2)
        return [_i(-1, ctx.reg(a[1]), 4, ctx.reg(a[0]), 0x13)]
    if op == "neg":
        _nargs(ctx, op, a, 2)
        return [_r(0x20, ctx.reg(a[1]), 0, 0, ctx.reg(a[0]), 0x33)]
    if op == "seqz":
        _nargs(ctx, op, a, 2)
        return [_i(1, ctx.reg(a[1]), 3, ctx.reg(a[0]), 0x13)]
    if op == "snez":
        _nargs(ctx, op, a, 2)
        return [_r(0, ctx.reg(a[1]), 0, 3, ctx.reg(a[0]), 0x33)]
    if op == "j":
        _nargs(ctx, op, a, 1)
        return [_jal(ctx, 0, a[0])]
    if op == "call":
        _nargs(ctx, op, a, 1)
        return [_jal(ctx, 1, a[0])]
    if op == "jr":
        _nargs(ctx, op, a, 1)
        return [_i(0, ctx.reg(a[0]), 0, 0, 0x67)]
    if op == "ret":
        _nargs(ctx, op, a, 0)
        return [_i(0, 1, 0, 0, 0x67)]
    raise AsmError(f"unknown instruction {op!r}", ctx.lineno)
