// CrossLayer stub for learn-fpga's clockworks.v (NOT the upstream file).
//
// pipeline6.v includes "clockworks.v" for its board-level SOC wrapper. The
// upstream file includes "../../RTL/PLL/femtopll.v" (FPGA PLLs) and is only
// used by SOC, which the CrossLayer build never elaborates (core.yaml sets
// `top: tb`, so iverilog -s tb). This stub keeps the include resolvable with
// the same module interface: the clock passes through and reset is inverted,
// as upstream does in simulation without CPU_FREQ.

module Clockworks
(
   input  CLK,
   input  RESET,
   output clk,
   output resetn
);
   parameter SLOW=0;
   assign clk = CLK;
   assign resetn = !RESET;
endmodule
