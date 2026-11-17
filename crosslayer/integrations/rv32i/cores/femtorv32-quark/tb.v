// CrossLayer testbench for FemtoRV32 Quark (Icarus Verilog).
//
// Quark has no RISC-V Formal Interface (RVFI). A retirement monitor in this
// testbench reads the core's internal state through hierarchical references
// (the RTL is unmodified) and records each retired instruction in RVFI terms
// in the mon_* registers, pulsing mon_valid for one cycle. The core profile
// maps those registers for `trace: vcd-signals`; with +commit_log the monitor
// also prints Spike-style commit lines.
//
// Quark has no traps: it executes EBREAK and ECALL as cycle-counter reads into
// x0. This testbench ends the run when one of them retires and reports it as
// RVFI would (trap=1), matching the reference model.
//
// Plusargs: +hex=FILE +words=N +max_cycles=N +wait_states=N +vcd=FILE
//           +commit_log (+no_retire_lines is accepted; Quark prints none anyway)
// Marker lines (parsed by crosslayer/integrations/rv32i/traces): BOOT, OUT,
// BUSERR, HALT, TIMEOUT, as in cores/picorv32/tb.v.

`timescale 1 ns / 1 ps

module tb;
	localparam MEM_WORDS = 16384;
	localparam [31:0] OUT_ADDR = 32'h1000_0000;
	localparam [31:0] EBREAK = 32'h0010_0073;
	localparam [31:0] ECALL = 32'h0000_0073;

	reg clk = 0;
	reg reset = 0;  // active low
	always #5 clk = ~clk;

	reg [31:0] memory [0:MEM_WORDS-1];
	reg [1023:0] hex_file;
	reg [1023:0] vcd_file;
	integer max_cycles, wait_states, cycle, retired, words, i, shift, rwait, wwait;
	reg halting, commit_log;

	wire [31:0] mem_addr, mem_wdata;
	wire [3:0]  mem_wmask;
	wire        mem_rstrb;
	reg  [31:0] mem_rdata;
	reg  [31:0] raddr;
	reg         mem_rbusy = 0, mem_wbusy = 0;

	FemtoRV32 #(.RESET_ADDR(32'h0), .ADDR_WIDTH(32)) uut (
		.clk(clk), .mem_addr(mem_addr), .mem_wdata(mem_wdata), .mem_wmask(mem_wmask),
		.mem_rdata(mem_rdata), .mem_rstrb(mem_rstrb), .mem_rbusy(mem_rbusy), .mem_wbusy(mem_wbusy),
		.reset(reset)
	);

	initial begin
		for (i = 0; i < MEM_WORDS; i = i + 1) memory[i] = 0;
		for (i = 0; i < 32; i = i + 1) uut.registerFile[i] = 0;  // like PicoRV32's REGS_INIT_ZERO
		if (!$value$plusargs("hex=%s", hex_file)) hex_file = "program.hex";
		if (!$value$plusargs("max_cycles=%d", max_cycles)) max_cycles = 100000;
		if (!$value$plusargs("wait_states=%d", wait_states)) wait_states = 0;
		if (!$value$plusargs("words=%d", words)) words = MEM_WORDS;
		commit_log = $test$plusargs("commit_log");
		if ($value$plusargs("vcd=%s", vcd_file)) begin
			$dumpfile(vcd_file);
			$dumpvars(1, tb);
		end
		$readmemh(hex_file, memory, 0, words - 1);
		$display("BOOT max_cycles=%0d wait_states=%0d", max_cycles, wait_states);
		cycle = 0;
		retired = 0;
		rwait = 0;
		wwait = 0;
		halting = 0;
		repeat (10) @(posedge clk);
		reset <= 1;
	end

	// ---------------------------------------------------------------------
	// Memory: Quark's handshake. A read strobe returns data after
	// wait_states extra cycles (mem_rbusy high meanwhile); a write takes
	// effect at once and holds mem_wbusy for wait_states cycles.
	// ---------------------------------------------------------------------
	function [31:0] read_word(input [31:0] addr);
		begin
			if (addr == OUT_ADDR) read_word = 0;
			else if (addr < 4 * MEM_WORDS) read_word = memory[addr >> 2];
			else begin
				$display("BUSERR cyc=%0d addr=%08x", cycle, addr);
				read_word = 0;
			end
		end
	endfunction

	always @(posedge clk) begin
		if (reset && mem_rstrb) begin
			raddr <= mem_addr;
			if (wait_states == 0) mem_rdata <= read_word(mem_addr);
			else begin
				rwait <= wait_states;
				mem_rbusy <= 1;
			end
		end else if (rwait > 0) begin
			rwait <= rwait - 1;
			if (rwait == 1) begin
				mem_rbusy <= 0;
				mem_rdata <= read_word(raddr);
			end
		end
		if (reset && mem_wmask) begin
			if (mem_addr == OUT_ADDR) $display("OUT cyc=%0d data=%08x", cycle, mem_wdata);
			else if (mem_addr < 4 * MEM_WORDS) begin
				if (mem_wmask[0]) memory[mem_addr >> 2][ 7: 0] <= mem_wdata[ 7: 0];
				if (mem_wmask[1]) memory[mem_addr >> 2][15: 8] <= mem_wdata[15: 8];
				if (mem_wmask[2]) memory[mem_addr >> 2][23:16] <= mem_wdata[23:16];
				if (mem_wmask[3]) memory[mem_addr >> 2][31:24] <= mem_wdata[31:24];
			end else $display("BUSERR cyc=%0d addr=%08x", cycle, mem_addr);
			if (wait_states != 0) begin
				wwait <= wait_states;
				mem_wbusy <= 1;
			end
		end else if (wwait > 0) begin
			wwait <= wwait - 1;
			if (wwait == 1) mem_wbusy <= 0;
		end
	end

	// ---------------------------------------------------------------------
	// Retirement monitor. In EXECUTE the instruction, its PC, operands, next
	// PC and memory access are known; it retires at that edge unless it waits
	// (loads, stores, shifts), in which case it retires when WAIT_ALU_OR_MEM
	// ends, with the final write-back value.
	// ---------------------------------------------------------------------
	reg pending = 0;
	reg [31:0] p_pc, p_insn, p_rs1v, p_rs2v, p_npc, p_maddr, p_wdata;
	reg [4:0]  p_rs1, p_rs2;
	reg [3:0]  p_wmask;
	reg        p_trap;

	wire in_execute = uut.state[2];
	wire in_wait = uut.state[3];
	wire [31:0] insn = {uut.instr, 2'b11};
	wire uses_rs1 = !(uut.isLUI | uut.isAUIPC | uut.isJAL | uut.isSYSTEM);
	wire uses_rs2 = uut.isBranch | uut.isStore | uut.isALUreg;
	wire system_trap = uut.isSYSTEM && (insn == EBREAK || insn == ECALL);
	wire retire_now = reset && !halting && (
		(in_execute && (!uut.needToWait || system_trap)) ||
		(in_wait && pending && !uut.aluBusy && !mem_rbusy && !mem_wbusy));

	reg        mon_valid = 0;
	reg [63:0] mon_order = 0;
	reg [31:0] mon_cyc, mon_pc, mon_insn, mon_rs1v, mon_rs2v, mon_rdv, mon_npc, mon_maddr, mon_wdata;
	reg [4:0]  mon_rs1, mon_rs2, mon_rd;
	reg [3:0]  mon_wmask;
	reg        mon_trap;

	// What EXECUTE knows about the instruction, in RVFI conventions: unused
	// operands read as 0, data addresses are word-aligned, loads have no mask.
	always @(posedge clk) begin
		if (reset && in_execute) begin
			p_pc <= uut.PC;
			p_insn <= insn;
			p_rs1 <= uses_rs1 ? insn[19:15] : 5'd0;
			p_rs2 <= uses_rs2 ? insn[24:20] : 5'd0;
			p_rs1v <= uses_rs1 && insn[19:15] != 0 ? uut.rs1 : 32'd0;
			p_rs2v <= uses_rs2 && insn[24:20] != 0 ? uut.rs2 : 32'd0;
			p_trap <= system_trap;
			p_npc <= system_trap ? uut.PC :
			         uut.isJALR ? {uut.aluPlus[31:1], 1'b0} :
			         uut.jumpToPCplusImm ? uut.PCplusImm : uut.PCplus4;
			p_maddr <= (uut.isLoad | uut.isStore) && !system_trap ? {uut.loadstore_addr[31:2], 2'b00} : 32'd0;
			p_wmask <= uut.isStore ? mem_wmask : 4'd0;
			p_wdata <= uut.isStore ? mem_wdata : 32'd0;
			pending <= uut.needToWait && !system_trap;
		end else if (retire_now) pending <= 0;
	end

	always @(posedge clk) begin
		mon_valid <= 0;
		if (retire_now) begin
			mon_valid <= 1;
			mon_cyc <= cycle;
			mon_order <= retired;
			retired = retired + 1;
			if (in_execute) begin  // retires in the cycle it executes
				mon_pc <= uut.PC;
				mon_insn <= insn;
				mon_rs1 <= uses_rs1 ? insn[19:15] : 5'd0;
				mon_rs2 <= uses_rs2 ? insn[24:20] : 5'd0;
				mon_rs1v <= uses_rs1 && insn[19:15] != 0 ? uut.rs1 : 32'd0;
				mon_rs2v <= uses_rs2 && insn[24:20] != 0 ? uut.rs2 : 32'd0;
				mon_trap <= system_trap;
				mon_npc <= system_trap ? uut.PC :
				           uut.isJALR ? {uut.aluPlus[31:1], 1'b0} :
				           uut.jumpToPCplusImm ? uut.PCplusImm : uut.PCplus4;
				mon_maddr <= 32'd0;
				mon_wmask <= 4'd0;
				mon_wdata <= 32'd0;
			end else begin  // retires at the end of WAIT_ALU_OR_MEM
				mon_pc <= p_pc;
				mon_insn <= p_insn;
				mon_rs1 <= p_rs1;
				mon_rs2 <= p_rs2;
				mon_rs1v <= p_rs1v;
				mon_rs2v <= p_rs2v;
				mon_trap <= p_trap;
				mon_npc <= p_npc;
				mon_maddr <= p_maddr;
				mon_wmask <= p_wmask;
				mon_wdata <= p_wdata;
			end
			mon_rd <= !system_trap && uut.writeBack && uut.rdId != 0 ? uut.rdId : 5'd0;
			mon_rdv <= !system_trap && uut.writeBack && uut.rdId != 0 ? uut.writeBackData : 32'd0;
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

	// Report one cycle after retirement (when mon_* hold it, which is also
	// when a waveform sampler sees mon_valid), then stop on a trap or limit.
	always @(posedge clk) begin
		if (reset) begin
			cycle <= cycle + 1;
			if (mon_valid && !halting) begin
				if (commit_log) print_commit;
				if (mon_trap) begin
					halting = 1;
					$display("HALT cyc=%0d retired=%0d", cycle, retired);
					$finish(0);
				end
			end
			if (!halting && cycle >= max_cycles) begin
				halting = 1;
				$display("TIMEOUT cyc=%0d retired=%0d", cycle, retired);
				$finish(0);
			end
		end
	end
endmodule
