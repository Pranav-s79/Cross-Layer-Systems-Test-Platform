# selftest.s: exercises every RV32I instruction the reference core runs and
# writes results to the output port. The reference model and the RTL must
# produce the same outputs; tests/integrations/test_rv32i.py lists them.

        .equ OUT, 0x10000000

start:
        li    sp, 0x8000            # stack grows down from 32 KB
        li    s11, OUT              # output port

# -- 1. arithmetic and logic ---------------------------------------------------------
        li    a0, 1000
        li    a1, -7
        add   t0, a0, a1            # 993
        sw    t0, 0(s11)
        sub   t0, a0, a1            # 1007
        sw    t0, 0(s11)
        sub   t0, a1, a0            # -1007
        sw    t0, 0(s11)
        xor   t0, a0, a1
        or    t1, a0, a1
        and   t2, a0, a1
        add   t0, t0, t1
        add   t0, t0, t2            # xor + or + and
        sw    t0, 0(s11)
        xori  t0, a1, 0x55
        ori   t1, a0, -256
        andi  t2, a1, 0x7f0
        add   t0, t0, t1
        add   t0, t0, t2
        sw    t0, 0(s11)

# -- 2. comparisons: signed and unsigned disagree on negative numbers ------------------
        slt   t0, a1, a0            # 1  (-7 < 1000)
        sltu  t1, a1, a0            # 0  (0xfffffff9 > 1000)
        slti  t2, a1, 0             # 1
        sltiu t3, a1, 5             # 0
        slli  t1, t1, 1
        slli  t2, t2, 2
        slli  t3, t3, 3
        or    t0, t0, t1
        or    t0, t0, t2
        or    t0, t0, t3            # 0b0101 = 5
        sw    t0, 0(s11)

# -- 3. shifts by immediate and by register ---------------------------------------------
        li    a2, 0x80000F0F
        slli  t0, a2, 4             # 0x0000f0f0
        srli  t1, a2, 4             # 0x080000f0
        srai  t2, a2, 4             # 0xf80000f0
        sw    t0, 0(s11)
        sw    t1, 0(s11)
        sw    t2, 0(s11)
        li    a3, 31
        sll   t0, a0, a3
        srl   t1, a2, a3            # 1
        sra   t2, a2, a3            # -1
        add   t0, t0, t1
        add   t0, t0, t2
        sw    t0, 0(s11)

# -- 4. upper immediates and x0 ------------------------------------------------------------
        lui   t0, 0xABCDE           # 0xabcde000
        sw    t0, 0(s11)
        auipc t1, 0
        la    t2, start
        sub   t1, t1, t2            # address of the auipc above
        sw    t1, 0(s11)
        addi  x0, x0, 5             # writes to x0 are ignored
        add   t0, x0, x0            # 0
        sw    t0, 0(s11)

# -- 5. branches: each kind taken and not taken -------------------------------------------
        li    s0, 0                 # bit i set when check i behaves correctly
        li    a4, 5
        li    a5, 5
        li    a6, -3
        beq   a4, a5, 1f
        j     2f
1:      ori   s0, s0, 1
2:      bne   a4, a5, 3f            # not taken
        ori   s0, s0, 2
3:      bne   a4, a6, 4f            # taken
        j     5f
4:      ori   s0, s0, 4
5:      blt   a6, a4, 6f            # taken: -3 < 5 signed
        j     7f
6:      ori   s0, s0, 8
7:      bltu  a6, a4, 8f            # not taken: 0xfffffffd > 5 unsigned
        ori   s0, s0, 16
8:      bge   a4, a6, 9f            # taken
        j     10f
9:      ori   s0, s0, 32
10:     bgeu  a4, a6, 11f           # not taken
        ori   s0, s0, 64
11:     beq   a4, a6, 12f           # not taken
        ori   s0, s0, 128
12:     sw    s0, 0(s11)            # 255 when every branch behaves

# -- 6. calls and indirect jumps -------------------------------------------------------------
        li    a0, 12
        call  square                # a0 = 144
        sw    a0, 0(s11)
        la    t0, square
        li    a0, 9
        jalr  ra, 0(t0)             # a0 = 81
        sw    a0, 0(s11)

# -- 7. memory: every byte lane, both halfwords, sign and zero extension -----------------
        la    s1, buffer
        li    t0, 0x11223344
        sw    t0, 0(s1)
        li    t1, 0xAA
        sb    t1, 1(s1)             # 0x1122aa44
        li    t1, 0xBB
        sb    t1, 2(s1)             # 0x11bbaa44
        li    t1, 0xCC
        sb    t1, 3(s1)             # 0xccbbaa44
        lw    t2, 0(s1)
        sw    t2, 0(s11)
        li    t1, 0x8001
        sh    t1, 4(s1)
        li    t1, 0x7FFE
        sh    t1, 6(s1)             # word 1 = 0x7ffe8001
        lw    t2, 4(s1)
        sw    t2, 0(s11)
        lb    t3, 3(s1)             # 0xffffffcc
        lbu   t4, 3(s1)             # 0x000000cc
        lh    t5, 4(s1)             # 0xffff8001
        lhu   t6, 4(s1)             # 0x00008001
        sw    t3, 0(s11)
        sw    t4, 0(s11)
        sw    t5, 0(s11)
        sw    t6, 0(s11)

# -- 8. loops: sum 1..10, then Fibonacci ---------------------------------------------------
        li    t0, 10
        li    t1, 0
sum_loop:
        add   t1, t1, t0
        addi  t0, t0, -1
        bnez  t0, sum_loop          # 55
        sw    t1, 0(s11)
        li    t0, 20                # fib(20)
        li    t1, 0
        li    t2, 1
fib_loop:
        beqz  t0, fib_done
        add   t3, t1, t2
        mv    t1, t2
        mv    t2, t3
        addi  t0, t0, -1
        j     fib_loop
fib_done:
        sw    t1, 0(s11)            # 6765

# -- 9. bubble sort of eight words in memory ----------------------------------------------
        la    s2, data
        li    s3, 8
outer:
        addi  s3, s3, -1
        blez  s3, sorted
        mv    t0, s2
        mv    t1, s3
inner:
        lw    t2, 0(t0)
        lw    t3, 4(t0)
        ble   t2, t3, no_swap
        sw    t3, 0(t0)
        sw    t2, 4(t0)
no_swap:
        addi  t0, t0, 4
        addi  t1, t1, -1
        bnez  t1, inner
        j     outer
sorted:
        li    t0, 8
        mv    t1, s2
emit:
        lw    t2, 0(t1)
        sw    t2, 0(s11)
        addi  t1, t1, 4
        addi  t0, t0, -1
        bnez  t0, emit

        ebreak

square:                             # a0 = a0 * a0 by repeated addition
        mv    t0, a0
        li    t1, 0
1:      add   t1, t1, a0
        addi  t0, t0, -1
        bnez  t0, 1b
        mv    a0, t1
        ret

        .align 4
data:   .word 42, -5, 17, 0, 99, -100, 3, 8
buffer: .space 16
