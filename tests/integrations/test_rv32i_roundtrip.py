"""Assembler and decoder agree: ``decode(assemble(text))`` returns the operands, and
every reserved or unsupported encoding decodes as illegal."""

from __future__ import annotations

import random

import pytest

from crosslayer.integrations.rv32i.asm import assemble
from crosslayer.integrations.rv32i.iss import MNEMONICS, Decoded, decode

_R_OPS = ("add", "sub", "sll", "slt", "sltu", "xor", "srl", "sra", "or", "and")
_I_OPS = ("addi", "slti", "sltiu", "xori", "ori", "andi")
_SHIFTS = ("slli", "srli", "srai")
_LOADS = ("lb", "lh", "lw", "lbu", "lhu")
_STORES = ("sb", "sh", "sw")
_BRANCHES = ("beq", "bne", "blt", "bge", "bltu", "bgeu")
_SEEDS = range(40)


def _one(text: str) -> Decoded:
    program = assemble(text)
    assert len(program.words) == 1
    return decode(program.words[0])


def _check(d: Decoded, mnemonic: str, kind: str, **fields: int) -> None:
    assert (d.mnemonic, d.kind) == (mnemonic, kind)
    for name, value in fields.items():
        assert getattr(d, name) == value, name


@pytest.mark.parametrize("seed", _SEEDS)
def test_random_instructions_round_trip(seed: int) -> None:
    rng = random.Random(seed)
    reg = lambda: rng.randrange(32)  # noqa: E731
    for _ in range(25):
        rd, rs1, rs2 = reg(), reg(), reg()
        op = rng.choice(_R_OPS)
        _check(_one(f"{op} x{rd}, x{rs1}, x{rs2}"), op, "alu", rd=rd, rs1=rs1, rs2=rs2)
        op, imm = rng.choice(_I_OPS), rng.randint(-2048, 2047)
        _check(_one(f"{op} x{rd}, x{rs1}, {imm}"), op, "alu", rd=rd, rs1=rs1, imm=imm)
        op, shamt = rng.choice(_SHIFTS), rng.randrange(32)
        _check(_one(f"{op} x{rd}, x{rs1}, {shamt}"), op, "alu", rd=rd, rs1=rs1, imm=shamt)
        op, imm = rng.choice(_LOADS), rng.randint(-2048, 2047)
        _check(_one(f"{op} x{rd}, {imm}(x{rs1})"), op, "load", rd=rd, rs1=rs1, imm=imm)
        op, imm = rng.choice(_STORES), rng.randint(-2048, 2047)
        _check(_one(f"{op} x{rs2}, {imm}(x{rs1})"), op, "store", rs1=rs1, rs2=rs2, imm=imm)
        op, imm = rng.choice(_BRANCHES), rng.randrange(-2048, 2048) * 2
        _check(_one(f"{op} x{rs1}, x{rs2}, {imm}"), op, "branch", rs1=rs1, rs2=rs2, imm=imm)
        imm = rng.randrange(-(1 << 19), 1 << 19) * 2
        _check(_one(f"jal x{rd}, {imm}"), "jal", "jal", rd=rd, imm=imm)
        imm = rng.randint(-2048, 2047)
        _check(_one(f"jalr x{rd}, x{rs1}, {imm}"), "jalr", "jalr", rd=rd, rs1=rs1, imm=imm)
        upper = rng.randrange(1 << 20)
        signed = (upper << 12) - (1 << 32) if upper >> 19 else upper << 12
        for op in ("lui", "auipc"):
            _check(_one(f"{op} x{rd}, {upper}"), op, "upper", rd=rd, imm=signed)


def test_system_instructions_round_trip() -> None:
    _check(_one("ecall"), "ecall", "system")
    _check(_one("ebreak"), "ebreak", "system")


def test_every_modeled_mnemonic_is_covered() -> None:
    covered = {*_R_OPS, *_I_OPS, *_SHIFTS, *_LOADS, *_STORES, *_BRANCHES,
               "lui", "auipc", "jal", "jalr", "ecall", "ebreak"}
    assert covered == set(MNEMONICS)


def _enc_r(f7: int, f3: int, opcode: int = 0x33) -> int:
    return (f7 << 25) | (3 << 20) | (2 << 15) | (f3 << 12) | (1 << 7) | opcode


def _reserved_words() -> list[tuple[str, int]]:
    words: list[tuple[str, int]] = [("zero word", 0), ("all ones", 0xFFFFFFFF), ("compressed quadrant", 0x0000_4501)]
    valid_r = {(0x00, f3) for f3 in range(8)} | {(0x20, 0), (0x20, 5)}
    for f7 in (0x01, 0x02, 0x10, 0x20, 0x7F):
        for f3 in range(8):
            if (f7, f3) not in valid_r:
                words.append((f"OP f7={f7:#x} f3={f3}", _enc_r(f7, f3)))
    for f7 in (0x01, 0x10, 0x7F):  # shift-immediate with reserved funct7 (and srai-style on slli)
        for f3 in (1, 5):
            words.append((f"OP-IMM shift f7={f7:#x} f3={f3}", _enc_r(f7, f3, 0x13)))
    words.append(("slli with srai funct7", _enc_r(0x20, 1, 0x13)))
    for f3 in (3, 6, 7):  # LOAD
        words.append((f"LOAD f3={f3}", (1 << 15) | (f3 << 12) | (1 << 7) | 0x03))
    for f3 in (3, 4, 5, 6, 7):  # STORE
        words.append((f"STORE f3={f3}", (1 << 15) | (f3 << 12) | 0x23))
    for f3 in (2, 3):  # BRANCH
        words.append((f"BRANCH f3={f3}", (1 << 15) | (f3 << 12) | 0x63))
    for f3 in range(1, 8):  # JALR needs funct3 = 0
        words.append((f"JALR f3={f3}", (1 << 15) | (f3 << 12) | (1 << 7) | 0x67))
    words += [
        ("fence", 0x0FF0000F),
        ("csrrw", 0x3400_1073),
        ("csrrs", 0x3000_2073),
        ("mret", 0x3020_0073),
        ("wfi", 0x1050_0073),
        ("ecall with rd set", 0x0000_00F3),
        ("ebreak with rs1 set", 0x0010_8073),
        ("AMO opcode", 0x0000_302F),
        ("OP-FP opcode", 0x0000_0053),
        ("custom-0 opcode", 0x0000_000B),
    ]
    return words


@pytest.mark.parametrize(("label", "word"), _reserved_words(), ids=[w[0] for w in _reserved_words()])
def test_reserved_encodings_decode_as_illegal(label: str, word: int) -> None:
    d = decode(word)
    assert (d.mnemonic, d.kind) == ("illegal", "illegal"), label


def test_random_words_decode_consistently() -> None:
    """Every decoded non-illegal word names a modeled mnemonic; the rest are illegal."""
    rng = random.Random(7)
    seen_illegal = False
    for _ in range(5000):
        d = decode(rng.randrange(1 << 32))
        if d.kind == "illegal":
            seen_illegal = True
            assert d.mnemonic == "illegal"
        else:
            assert d.mnemonic in MNEMONICS
    assert seen_illegal


def test_model_halts_on_illegal_encoding() -> None:
    from crosslayer.integrations.rv32i.iss import Model

    image = assemble("addi x5, x0, 1").image() + (0x3400_1073).to_bytes(4, "little")
    run = Model(image).run()
    assert run.halt == "illegal"
    assert [r.mnemonic for r in run.retires][0] == "addi"
    assert run.retires[-1].trap
