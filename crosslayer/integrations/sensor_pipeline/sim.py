"""Reference system under test: a simulated sensor -> ring buffer -> processor pipeline.

This is a deterministic discrete-event *simulation*, not firmware. It exists so
the CrossLayer core can be exercised end to end, with faults whose location is
known in advance. It deliberately imports nothing from ``crosslayer``.
CrossLayer only sees the log it prints, the same way it would see a real
device's UART or dmesg output.

Model
-----
* A sensor delivers packets with exponentially distributed inter-arrival
  times. Each packet carries a sequence number and an 8-byte payload protected
  by CRC-8.
* The RX path (``sensor_rx``) hands packets to the consumer (``proc``). If the
  consumer is busy, the packet waits in a ring buffer of fixed capacity. If the
  buffer is full, the packet is dropped and the drop is logged (policy: drops
  are allowed but must be explicit).
* ``proc`` services one packet at a time (uniform service time), recomputes
  the CRC, and logs the result with the packet's latency.

Arrivals and service times use separate random streams. Enabling a fault that
changes service behavior therefore does not change the incoming traffic, so a
faulty run can be compared with its baseline packet by packet.

Injectable faults (``--fault``)
-------------------------------
silent_drop           overflow path discards packets without logging
slow_consumer         service time multiplied by --slow-factor
corrupt_checksum      one bit flipped in every Nth packet that waits in the buffer
duplicate_completion  every Nth completion is reported twice

Log format (one record per line, times in seconds with microsecond resolution)::

    [    0.001234] sensor_rx: RX seq=1 len=8 crc=0x3a
    [    0.001450] proc: DONE seq=1 crc_rx=0x3a crc_calc=0x3a lat_us=216
"""

from __future__ import annotations

import argparse
import heapq
import itertools
import random
import sys
from collections import deque
from dataclasses import dataclass
from typing import TextIO

FIRMWARE = "refpipe-1.0"
PAYLOAD_BYTES = 8
FAULTS = ("silent_drop", "slow_consumer", "corrupt_checksum", "duplicate_completion")


def crc8(data: bytes) -> int:
    """CRC-8 with polynomial 0x07 (CRC-8/SMBUS), MSB first."""
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


@dataclass
class Packet:
    seq: int
    arrival_us: int
    payload: bytearray
    crc: int


class Logger:
    def __init__(self, out: TextIO) -> None:
        self.out = out

    def __call__(self, t_us: int, subsystem: str, verb: str, **fields: object) -> None:
        stamp = f"[{t_us // 1_000_000:5d}.{t_us % 1_000_000:06d}]"
        body = " ".join(f"{k}={v}" for k, v in fields.items())
        self.out.write(f"{stamp} {subsystem}: {verb} {body}".rstrip() + "\n")


def simulate(args: argparse.Namespace, out: TextIO) -> None:
    faults = set(args.fault)
    arrivals = random.Random(args.seed * 1000 + 1)
    service = random.Random(args.seed * 1000 + 2)
    payloads = random.Random(args.seed * 1000 + 3)
    log = Logger(out)

    log(0, "sys", "BOOT", fw=FIRMWARE, seed=args.seed, cap=args.buffer_capacity)

    order = itertools.count()
    heap: list[tuple[int, int, str, Packet]] = []
    t = 1_000  # first packet no earlier than 1 ms after boot
    for seq in range(1, args.packets + 1):
        t += max(1, round(arrivals.expovariate(1 / args.mean_interarrival_us)))
        payload = bytearray(payloads.randrange(256) for _ in range(PAYLOAD_BYTES))
        heapq.heappush(heap, (t, next(order), "rx", Packet(seq, t, payload, crc8(payload))))

    scale = args.slow_factor if "slow_consumer" in faults else 1
    buffer: deque[Packet] = deque()
    busy = False
    rx = done = dropped_logged = buffered = completions = 0
    now = 0

    def start_service(t_us: int, pkt: Packet) -> None:
        nonlocal busy
        busy = True
        duration = service.randint(args.service_us_min, args.service_us_max) * scale
        heapq.heappush(heap, (t_us + duration, next(order), "done", pkt))

    while heap:
        now, _, kind, pkt = heapq.heappop(heap)
        if kind == "rx":
            rx += 1
            log(now, "sensor_rx", "RX", seq=pkt.seq, len=len(pkt.payload), crc=f"0x{pkt.crc:02x}")
            if not busy:
                start_service(now, pkt)
            elif len(buffer) < args.buffer_capacity:
                buffered += 1
                if "corrupt_checksum" in faults and buffered % args.corrupt_every == 0:
                    pkt.payload[3] ^= 0x01  # single-bit upset while the packet sits in RAM
                buffer.append(pkt)
            elif "silent_drop" not in faults:
                dropped_logged += 1
                log(now, "sensor_rx", "DROP", seq=pkt.seq, reason="buffer_full", occ=len(buffer))
            # with silent_drop the packet is discarded with no record at all
        else:
            done += 1
            completions += 1
            fields = dict(
                seq=pkt.seq,
                crc_rx=f"0x{pkt.crc:02x}",
                crc_calc=f"0x{crc8(bytes(pkt.payload)):02x}",
                lat_us=now - pkt.arrival_us,
            )
            log(now, "proc", "DONE", **fields)
            if "duplicate_completion" in faults and completions % args.duplicate_every == 0:
                log(now, "proc", "DONE", **fields)  # completion reported twice
            busy = False
            if buffer:
                start_service(now, buffer.popleft())

    log(now + 100, "sys", "HALT", rx=rx, done=done, drop=dropped_logged)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--seed", type=int, required=True)
    p.add_argument("--packets", type=int, default=400)
    p.add_argument("--buffer-capacity", type=int, default=8)
    p.add_argument("--mean-interarrival-us", type=int, default=250)
    p.add_argument("--service-us-min", type=int, default=100)
    p.add_argument("--service-us-max", type=int, default=300)
    p.add_argument("--fault", action="append", default=[], choices=FAULTS)
    p.add_argument("--slow-factor", type=int, default=3)
    p.add_argument("--corrupt-every", type=int, default=25)
    p.add_argument("--duplicate-every", type=int, default=40)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    sys.stdout.reconfigure(newline="\n")  # identical bytes on Windows and Linux
    simulate(args, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
