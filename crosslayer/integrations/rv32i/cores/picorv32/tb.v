// CrossLayer testbench for PicoRV32 (Icarus Verilog).
//
// Loads a program into 64 KB of memory, releases reset, and prints one line
// per retired instruction from the core's RISC-V Formal Interface (RVFI).
// Stores to OUT_ADDR are program output. The run ends on a trap (EBREAK,
// illegal instruction) or after +max_cycles.
//
// Plusargs:
//   +hex=FILE          program image for $readmemh (default program.hex)
//   +words=N           number of words in the image (default: fill memory)
//   +max_cycles=N      cycle limit (default 100000)
//   +wait_states=N     extra cycles before memory answers (default 0)
//   +vcd=FILE          write a waveform of the testbench and core top level
//
// Output lines (parsed by crosslayer/integrations/rv32i/adapter.py):
//   BOOT    max_cycles=.. wait_states=..
//   RET     cyc=.. ord=.. pc=.. insn=.. rs1=.. rs1v=.. rs2=.. rs2v=.. rd=.. rdv=..
//           npc=.. maddr=.. wmask=.. wdata=.. trap=..
//   OUT     cyc=.. data=..
//   BUSERR  cyc=.. addr=..
//   HALT    cyc=.. retired=..
//   TIMEOUT cyc=.. retired=..

`timescale 1 ns / 1 ps

module tb;
	localparam MEM_WORDS = 16384;
	localparam [31:0] OUT_ADDR = 32'h1000_0000;

	reg clk = 0;
	reg resetn = 0;
	always #5 clk = ~clk;  // 10 ns clock period

	reg [31:0] memory [0:MEM_WORDS-1];
	reg [1023:0] hex_file;
	reg [1023:0] vcd_file;
	integer max_cycles, wait_states, wait_count, cycle, retired, words, i;
	reg halting;

	wire        mem_valid, mem_instr;
	reg         mem_ready;
	wire [31:0] mem_addr, mem_wdata;
	wire [ 3:0] mem_wstrb;
	reg  [31:0] mem_rdata;
	wire        trap;

	wire        rvfi_valid, rvfi_trap;
	wire [63:0] rvfi_order;
	wire [31:0] rvfi_insn, rvfi_pc_rdata, rvfi_pc_wdata;
	wire [ 4:0] rvfi_rs1_addr, rvfi_rs2_addr, rvfi_rd_addr;
	wire [31:0] rvfi_rs1_rdata, rvfi_rs2_rdata, rvfi_rd_wdata;
	wire [31:0] rvfi_mem_addr, rvfi_mem_rdata, rvfi_mem_wdata;
	wire [ 3:0] rvfi_mem_rmask, rvfi_mem_wmask;

	picorv32 #(
		.REGS_INIT_ZERO(1),
		.CATCH_MISALIGN(1),
		.CATCH_ILLINSN(1)
	) uut (
		.clk(clk), .resetn(resetn), .trap(trap),
		.mem_valid(mem_valid), .mem_instr(mem_instr), .mem_ready(mem_ready),
		.mem_addr(mem_addr), .mem_wdata(mem_wdata), .mem_wstrb(mem_wstrb), .mem_rdata(mem_rdata),
		.pcpi_wr(1'b0), .pcpi_rd(32'b0), .pcpi_wait(1'b0), .pcpi_ready(1'b0),
		.irq(32'b0),
		.rvfi_valid(rvfi_valid), .rvfi_order(rvfi_order), .rvfi_insn(rvfi_insn),
		.rvfi_trap(rvfi_trap),
		.rvfi_rs1_addr(rvfi_rs1_addr), .rvfi_rs2_addr(rvfi_rs2_addr),
		.rvfi_rs1_rdata(rvfi_rs1_rdata), .rvfi_rs2_rdata(rvfi_rs2_rdata),
		.rvfi_rd_addr(rvfi_rd_addr), .rvfi_rd_wdata(rvfi_rd_wdata),
		.rvfi_pc_rdata(rvfi_pc_rdata), .rvfi_pc_wdata(rvfi_pc_wdata),
		.rvfi_mem_addr(rvfi_mem_addr), .rvfi_mem_rmask(rvfi_mem_rmask), .rvfi_mem_wmask(rvfi_mem_wmask),
		.rvfi_mem_rdata(rvfi_mem_rdata), .rvfi_mem_wdata(rvfi_mem_wdata)
	);

	initial begin
		for (i = 0; i < MEM_WORDS; i = i + 1) memory[i] = 0;
		if (!$value$plusargs("hex=%s", hex_file)) hex_file = "program.hex";
		if (!$value$plusargs("max_cycles=%d", max_cycles)) max_cycles = 100000;
		if (!$value$plusargs("wait_states=%d", wait_states)) wait_states = 0;
		if (!$value$plusargs("words=%d", words)) words = MEM_WORDS;
		if ($value$plusargs("vcd=%s", vcd_file)) begin
			$dumpfile(vcd_file);
			$dumpvars(1, tb);
			$dumpvars(1, tb.uut);
		end
		$readmemh(hex_file, memory, 0, words - 1);
		$display("BOOT max_cycles=%0d wait_states=%0d", max_cycles, wait_states);
		cycle = 0;
		retired = 0;
		wait_count = 0;
		halting = 0;
		repeat (10) @(posedge clk);
		resetn <= 1;
	end

	// Memory: answers after wait_states extra cycles; OUT_ADDR is an output port.
	always @(posedge clk) begin
		mem_ready <= 0;
		if (resetn && mem_valid && !mem_ready) begin
			if (wait_count < wait_states) begin
				wait_count <= wait_count + 1;
			end else begin
				wait_count <= 0;
				mem_ready <= 1;
				mem_rdata <= 0;
				if (mem_addr == OUT_ADDR) begin
					if (mem_wstrb) $display("OUT cyc=%0d data=%08x", cycle, mem_wdata);
				end else if (mem_addr < 4 * MEM_WORDS) begin
					mem_rdata <= memory[mem_addr >> 2];
					if (mem_wstrb[0]) memory[mem_addr >> 2][ 7: 0] <= mem_wdata[ 7: 0];
					if (mem_wstrb[1]) memory[mem_addr >> 2][15: 8] <= mem_wdata[15: 8];
					if (mem_wstrb[2]) memory[mem_addr >> 2][23:16] <= mem_wdata[23:16];
					if (mem_wstrb[3]) memory[mem_addr >> 2][31:24] <= mem_wdata[31:24];
				end else begin
					$display("BUSERR cyc=%0d addr=%08x", cycle, mem_addr);
				end
			end
		end
	end

	// Trace: one line per retired instruction, then stop on a trap or the cycle limit.
	always @(posedge clk) begin
		if (resetn) begin
			cycle <= cycle + 1;
			if (rvfi_valid && !halting) begin
				retired = retired + 1;
				$display("RET cyc=%0d ord=%0d pc=%08x insn=%08x rs1=%0d rs1v=%08x rs2=%0d rs2v=%08x rd=%0d rdv=%08x npc=%08x maddr=%08x wmask=%x wdata=%08x trap=%0d",
					cycle, rvfi_order, rvfi_pc_rdata, rvfi_insn,
					rvfi_rs1_addr, rvfi_rs1_rdata, rvfi_rs2_addr, rvfi_rs2_rdata,
					rvfi_rd_addr, rvfi_rd_wdata, rvfi_pc_wdata,
					rvfi_mem_addr, rvfi_mem_wmask, rvfi_mem_wdata, rvfi_trap);
				if (rvfi_trap) begin
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
