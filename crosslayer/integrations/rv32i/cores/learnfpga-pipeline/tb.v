// CrossLayer testbench for the learn-fpga 5-stage pipelined RV32I core
// (pipeline6.v, module Processor), for Icarus Verilog.
//
// The core is a classic F/D/E/M/W pipeline: operands are forwarded to E from
// M and W, a load followed by a use of its result stalls F and D for one
// cycle, and a taken jump or branch (resolved in E) flushes D and F. It has
// no RISC-V Formal Interface, so a retirement monitor here reads its pipeline
// registers through hierarchical references (the RTL is unmodified):
//
// * E: the instruction's operands as actually used (after forwarding) and
//   its next PC. The core never stalls E, M or W, so two shadow stages
//   (em_*, mw_*) carry these values along with the instruction.
// * M: the data address, store mask and store data.
// * W: the instruction retires if MW_instr is not the core's bubble
//   (add x0, x0, x0), exactly the rule the core's own instret counter uses;
//   the write-back register and value are taken here.
//
// The core halts (stalls F and D) when EBREAK or ECALL reaches E; the monitor
// reports it as a trap when it retires, as RVFI would, and ends the run.
//
// Memory. The core has separate instruction (PROGROM) and data (DATARAM)
// memories of 64 KB each, indexed by address bits [15:2], which it loads
// from PROGROM.hex and DATARAM.hex. Both get the program image, so code and
// initialized data share addresses as in the reference model;
// self-modifying code is not supported. Stores to
// OUT_ADDR are program output (the core also writes them to DATARAM, which
// aliases the address; no program reads it back). Accesses above 64 KB are
// reported as bus errors, as in the other testbenches.
//
// The core has no memory handshake, so +wait_states=N stretches every core
// cycle to N+1 testbench cycles by gating the core clock: a timing-only fault.
//
// Plusargs: +max_cycles=N +wait_states=N +vcd=FILE (+hex and +words are ignored)
//           +commit_log (Spike-style commit lines instead of RET lines)
//           +no_retire_lines (no retirement lines: the waveform is the trace)
// Output lines: BOOT, RET (as cores/picorv32/tb.v), OUT, BUSERR, HALT, TIMEOUT.

`timescale 1 ns / 1 ps

module tb;
	localparam MEM_WORDS = 16384;
	localparam [31:0] OUT_ADDR = 32'h1000_0000;
	localparam [31:0] EBREAK = 32'h0010_0073;
	localparam [31:0] NOP = 32'h0000_0033;  // the core's bubble: add x0, x0, x0

	reg clk = 0;
	always #5 clk = ~clk;  // 10 ns testbench clock
	reg resetn = 0;

	// Clock gating for wait states; core_en changes only while clk is low.
	reg core_en = 1;
	integer gap = 0;
	wire core_clk = clk & core_en;

	wire [31:0] io_addr, io_wdata;
	wire        io_wr;
	Processor uut (
		.clk(core_clk), .resetn(resetn), .IO_mem_addr(io_addr), .IO_mem_rdata(32'd0),
		.IO_mem_wdata(io_wdata), .IO_mem_wr(io_wr)
	);

	reg [1023:0] vcd_file;
	integer max_cycles, wait_states, cycle, retired, i, shift;
	reg halting, trap_seen, commit_log, retire_lines;

	initial begin
		if (!$value$plusargs("max_cycles=%d", max_cycles)) max_cycles = 100000;
		if (!$value$plusargs("wait_states=%d", wait_states)) wait_states = 0;
		commit_log = $test$plusargs("commit_log");
		retire_lines = !$test$plusargs("no_retire_lines");
		cycle = 0;
		retired = 0;
		halting = 0;
		trap_seen = 0;
		if ($value$plusargs("vcd=%s", vcd_file)) begin
			$dumpfile(vcd_file);
			$dumpvars(1, tb);
		end
		// The core loads PROGROM.hex and DATARAM.hex itself; core.yaml's
		// memory_images has the adapter write the program image under both
		// names, zero-padded to 16384 words.
		for (i = 0; i < 32; i = i + 1) uut.RegisterBank[i] = 0;  // like PicoRV32's REGS_INIT_ZERO
		$display("BOOT max_cycles=%0d wait_states=%0d", max_cycles, wait_states);
		repeat (10) @(posedge clk);
		resetn <= 1;
	end

	always @(negedge clk) begin
		if (gap == 0) begin
			core_en <= 1;
			gap <= wait_states;
		end else begin
			core_en <= 0;
			gap <= gap - 1;
		end
	end

	// ---------------------------------------------------------------------
	// Instruction classes (restated from the core's decoder functions).
	// ---------------------------------------------------------------------
	function is_load;   input [31:0] I; is_load   = I[6:0] == 7'b0000011; endfunction
	function is_store;  input [31:0] I; is_store  = I[6:0] == 7'b0100011; endfunction
	function is_branch; input [31:0] I; is_branch = I[6:0] == 7'b1100011; endfunction
	function is_alureg; input [31:0] I; is_alureg = I[6:0] == 7'b0110011; endfunction
	function is_system; input [31:0] I; is_system = I[6:0] == 7'b1110011; endfunction
	function no_rs1;    input [31:0] I;  // LUI, AUIPC, JAL, SYSTEM read no register
		no_rs1 = I[6:0] == 7'b0110111 || I[6:0] == 7'b0010111 || I[6:0] == 7'b1101111 || is_system(I);
	endfunction
	function [31:0] lane_bits; input [3:0] m;
		lane_bits = {{8{m[3]}}, {8{m[2]}}, {8{m[1]}}, {8{m[0]}}};
	endfunction

	// ---------------------------------------------------------------------
	// E: operands after forwarding, next PC, trap. In RVFI conventions an
	// operand the instruction does not read is register 0 with value 0.
	// ---------------------------------------------------------------------
	wire [31:0] e_insn = uut.DE_instr;
	wire        e_uses_rs1 = !no_rs1(e_insn);
	wire        e_uses_rs2 = is_alureg(e_insn) || is_branch(e_insn) || is_store(e_insn);
	wire        e_trap = is_system(e_insn) && e_insn[14:12] == 3'b000;  // the core halts on these

	reg [31:0] em_rs1v, em_rs2v, em_npc, mw_rs1v, mw_rs2v, mw_npc;
	reg [4:0]  em_rs1, em_rs2, mw_rs1, mw_rs2;
	reg        em_trap, mw_trap;
	reg [31:0] mw_maddr, mw_wdata;
	reg [3:0]  mw_wmask;

	// M: the data access of the instruction in EM.
	wire [31:0] m_insn = uut.EM_instr;
	wire        m_access = is_load(m_insn) || is_store(m_insn);
	wire [31:0] m_word = {uut.EM_addr[31:2], 2'b00};

	always @(posedge core_clk) begin
		em_rs1 <= e_uses_rs1 ? e_insn[19:15] : 5'd0;
		em_rs2 <= e_uses_rs2 ? e_insn[24:20] : 5'd0;
		em_rs1v <= e_uses_rs1 && e_insn[19:15] != 0 ? uut.E_rs1 : 32'd0;
		em_rs2v <= e_uses_rs2 && e_insn[24:20] != 0 ? uut.E_rs2 : 32'd0;
		em_trap <= e_trap;
		em_npc <= e_trap ? uut.DE_PC : uut.E_JumpOrBranch ? uut.E_JumpOrBranchAddr : uut.DE_PC + 32'd4;

		mw_rs1 <= em_rs1;
		mw_rs2 <= em_rs2;
		mw_rs1v <= em_rs1v;
		mw_rs2v <= em_rs2v;
		mw_trap <= em_trap;
		mw_npc <= em_npc;
		mw_maddr <= m_access && !em_trap ? m_word : 32'd0;
		mw_wmask <= is_store(m_insn) ? uut.M_STORE_wmask : 4'd0;
		mw_wdata <= is_store(m_insn) ? uut.M_STORE_data : 32'd0;

		if (resetn && !halting && m_access) begin
			if (m_word == OUT_ADDR) begin
				if (is_store(m_insn))
					$display("OUT cyc=%0d data=%08x", cycle, uut.M_STORE_data & lane_bits(uut.M_STORE_wmask));
			end else if (m_word >= 4 * MEM_WORDS) $display("BUSERR cyc=%0d addr=%08x", cycle, m_word);
		end
	end

	// ---------------------------------------------------------------------
	// W: retirement.
	// ---------------------------------------------------------------------
	reg        mon_valid = 0;
	reg [63:0] mon_order = 0;
	reg [31:0] mon_cyc, mon_pc, mon_insn, mon_rs1v, mon_rs2v, mon_rdv, mon_npc, mon_maddr, mon_wdata;
	reg [4:0]  mon_rs1, mon_rs2, mon_rd;
	reg [3:0]  mon_wmask;
	reg        mon_trap;

	always @(posedge core_clk) begin
		mon_valid <= 0;
		if (resetn && !trap_seen && uut.MW_instr != NOP) begin
			mon_valid <= 1;
			mon_cyc <= cycle;
			mon_order <= retired;
			retired = retired + 1;
			trap_seen = mw_trap;  // later copies of the halting instruction never retire
			mon_pc <= uut.MW_PC;
			mon_insn <= uut.MW_instr;
			mon_rs1 <= mw_rs1;
			mon_rs2 <= mw_rs2;
			mon_rs1v <= mw_rs1v;
			mon_rs2v <= mw_rs2v;
			mon_trap <= mw_trap;
			mon_npc <= mw_npc;
			mon_maddr <= mw_maddr;
			mon_wmask <= mw_wmask;
			mon_wdata <= mw_wdata;
			mon_rd <= !mw_trap && uut.wbEnable ? uut.wbRdId : 5'd0;
			mon_rdv <= !mw_trap && uut.wbEnable ? uut.wbData : 32'd0;
		end
	end

	// One Spike-style commit line from the monitor (see cores/picorv32/tb.v).
	task print_commit;
		begin
			if (mon_trap) begin
				if (mon_insn == EBREAK) $display("core   0: exception trap_breakpoint, epc 0x%08x", mon_pc);
				else $display("core   0: exception trap_machine_ecall, epc 0x%08x", mon_pc);
			end else begin
				shift = mon_wmask[0] ? 0 : mon_wmask[1] ? 1 : mon_wmask[2] ? 2 : mon_wmask[3] ? 3 : 0;
				$write("core   0: 3 0x%08x (0x%08x)", mon_pc, mon_insn);
				if (mon_rd != 0) $write(" x%0d 0x%08x", mon_rd, mon_rdv);
				if (mon_insn[6:2] == 5'b00000 || mon_insn[6:2] == 5'b01000) $write(" mem 0x%08x", mon_maddr + shift);
				case (mon_wmask)
					4'b0000: ;
					4'b1111: $write(" 0x%h", mon_wdata);
					4'b0011, 4'b1100: $write(" 0x%h", mon_wdata[8*shift +: 16]);
					default: $write(" 0x%h", mon_wdata[8*shift +: 8]);
				endcase
				$write("\n");
			end
		end
	endtask

	// Report one core cycle after retirement (when mon_* hold it, which is
	// also when a waveform sampler sees mon_valid), then stop on a trap.
	always @(posedge core_clk) begin
		if (resetn && mon_valid && !halting) begin
			if (commit_log) print_commit;
			else if (retire_lines)
				$display("RET cyc=%0d ord=%0d pc=%08x insn=%08x rs1=%0d rs1v=%08x rs2=%0d rs2v=%08x rd=%0d rdv=%08x npc=%08x maddr=%08x wmask=%x wdata=%08x trap=%0d",
					mon_cyc, mon_order, mon_pc, mon_insn, mon_rs1, mon_rs1v, mon_rs2, mon_rs2v, mon_rd, mon_rdv,
					mon_npc, mon_maddr, mon_wmask, mon_wdata, mon_trap);
			if (mon_trap) begin
				halting = 1;
				$display("HALT cyc=%0d retired=%0d", cycle, retired);
				$finish(0);
			end
		end
	end

	always @(posedge clk) begin
		if (resetn) begin
			cycle <= cycle + 1;
			if (!halting && cycle >= max_cycles) begin
				halting = 1;
				$display("TIMEOUT cyc=%0d retired=%0d", cycle, retired);
				$finish(0);
			end
		end
	end
endmodule
