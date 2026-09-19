#!/usr/bin/env python3
"""
sky_demux.py - Split a switched-antenna HackRF capture into per-antenna dwells.

Pairs with hackrf-sky13418-switch.patch. The firmware cycles the SKY13418
through N, E, S, W and a 50-ohm marker port, in step with the sample clock:

    hackrf_operacake -o 0 -m time \
        -t A1:20000 -t A2:20000 -t A3:20000 -t A4:20000 -t B1:2000
    hackrf_transfer -r capture.iq -f 2440000000 -s 20000000

This script:
  1. Reads the int8 interleaved I/Q file written by hackrf_transfer.
  2. Folds power over the switching period and fits the known N/E/S/W/marker
     schedule to it; the best fit (with the marker quietest) fixes the phase.
  3. Discards the first `settle` samples after every switch.
  4. Reports per-antenna power for each cycle and a simple
     amplitude-comparison bearing (0 deg = N, 90 deg = E).

The bearing is a starting point only. It assumes four matched directional
antennas pointing N/E/S/W and needs calibration on real hardware.

Requires: numpy
"""

from __future__ import annotations

import argparse
import csv
import io
import math
import sys
from dataclasses import dataclass, field
from typing import Iterator

import numpy as np

ANTENNAS = ("N", "E", "S", "W")
SKY13418_SWITCH_TIME_S = 2.2e-6  # datasheet max turn-on time


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Schedule:
    """Switching plan; must match the hackrf_operacake -t arguments."""

    dwell: int  # samples per antenna (A1..A4)
    marker: int  # samples on the marker port (B1)

    @property
    def period(self) -> int:
        return len(ANTENNAS) * self.dwell + self.marker

    def antenna_offset(self, i: int) -> int:
        """Offset of antenna i from the start of a cycle (cycle starts at N)."""
        return i * self.dwell

    @property
    def marker_offset(self) -> int:
        return len(ANTENNAS) * self.dwell


def default_settle(sample_rate: float) -> int:
    """Samples to drop after each switch.

    The switch itself settles in about 2.2 us, but the HackRF baseband filter
    rings for longer, so allow roughly 5 us (100 samples at 20 Msps).
    """
    return max(math.ceil(SKY13418_SWITCH_TIME_S * sample_rate),
               math.ceil(5e-6 * sample_rate))


# --------------------------------------------------------------------------- #
# I/O
# --------------------------------------------------------------------------- #
def read_iq_int8(path: str, chunk_samples: int) -> Iterator[np.ndarray]:
    """Yield complex64 (single-precision) chunks from a hackrf_transfer int8 I/Q file."""
    with open(path, "rb") as f:
        while True:
            raw = np.fromfile(f, dtype=np.int8, count=2 * chunk_samples)
            if raw.size < 2:
                return
            raw = raw[: raw.size - (raw.size % 2)].astype(np.float32) / 128.0
            yield (raw[0::2] + 1j * raw[1::2]).astype(np.complex64)


def write_iq_int8(path: str, iq: np.ndarray) -> None:
    """Write complex samples in hackrf_transfer int8 format (for testing)."""
    out = np.empty(2 * iq.size, dtype=np.int8)
    out[0::2] = np.clip(np.round(iq.real * 127), -127, 127)
    out[1::2] = np.clip(np.round(iq.imag * 127), -127, 127)
    out.tofile(path)


# --------------------------------------------------------------------------- #
# Synchronisation
# --------------------------------------------------------------------------- #
@dataclass
class PhaseEstimate:
    phase: int  # sample index (mod period) where an N dwell starts
    contrast_db: float  # loudest antenna dwell power / marker power
    ambiguous: bool = False  # several equally good alignments (antenna null
    #                          next to the marker); resolved using the prior

    @property
    def reliable(self) -> bool:
        return self.contrast_db >= 6.0


def estimate_phase(iq: np.ndarray, sched: Schedule, settle: int,
                   prior: int | None = None) -> PhaseEstimate:
    """Find the cycle phase by fitting the switching schedule to folded power.

    `prior` is the expected phase (relative to iq[0]). It is only used to pick
    between alignments that fit equally well.
    """
    p = sched.period
    cycles = iq.size // p
    if cycles < 1:
        raise ValueError("need at least one full switching period to sync")

    power = np.abs(iq[: cycles * p]) ** 2
    profile = power.reshape(cycles, p).mean(axis=0)

    def circ_sum(length: int) -> np.ndarray:
        """sum(profile[j : j + length]) for every j, wrapping around."""
        doubled = np.concatenate([profile, profile[:length]])
        csum = np.concatenate([[0.0], np.cumsum(doubled)])
        return csum[length : length + p] - csum[:p]

    # Fit the known piecewise-constant schedule (N, E, S, W, marker) at every
    # candidate phase. Minimising the squared error of a piecewise-constant
    # fit is the same as maximising sum(S_k^2 / L_k) over the segments, where
    # S_k is the power summed over segment k. Unequal segment lengths make
    # the fit unique, and the marker must be (near) the quietest segment.
    lengths = [sched.dwell] * len(ANTENNAS) + [sched.marker]
    offsets = [sched.antenna_offset(i) for i in range(len(ANTENNAS))]
    offsets.append(sched.marker_offset)
    sums = {length: circ_sum(length) for length in set(lengths)}

    j = np.arange(p)
    score = np.zeros(p)
    means = []
    for off, length in zip(offsets, lengths):
        seg_sum = sums[length][(j + off) % p]
        score += seg_sum**2 / length
        means.append(seg_sum / length)
    marker_mean = means[-1]
    # Allow 3 dB of slack so a dwell sitting in an antenna null (noise only,
    # like the marker) does not disqualify the true alignment.
    quietest = marker_mean <= 2.0 * np.min(means[:-1], axis=0)
    if quietest.any():
        score = np.where(quietest, score, -np.inf)

    # Squared error of each alignment; near-equal errors are ambiguous.
    sse = float(np.sum(profile**2)) - score
    best = int(np.argmax(score))
    # Treat alignments as tied only if their extra error is within a few
    # tens of per-sample noise variances (estimated from the best fit).
    sigma2 = max(sse[best], 0.0) / max(p - len(lengths), 1)
    near = np.flatnonzero(sse <= sse[best] + 30.0 * sigma2 + 1e-18)
    # Group neighbouring candidates (a few samples apart) into clusters.
    clusters, current = [], [int(near[0])]
    for idx in near[1:]:
        if idx - current[-1] <= settle:
            current.append(int(idx))
        else:
            clusters.append(current)
            current = [int(idx)]
    clusters.append(current)
    if len(clusters) > 1 and clusters[0][0] == 0 and clusters[-1][-1] == p - 1:
        clusters[0] = clusters.pop() + clusters[0]  # wraps around
    reps = [max(c, key=lambda k: score[k]) for c in clusters]
    ambiguous = len(reps) > 1
    if ambiguous and prior is not None:
        def dist(k: int) -> int:
            d = abs(k - prior) % p
            return min(d, p - d)
        phase = min(reps, key=dist)
    else:
        phase = best
    marker_start = (phase + sched.marker_offset) % p
    m = sched.marker

    # Marker power over its settled middle section.
    inner = (marker_start + settle + np.arange(max(m - 2 * settle, 1))) % p
    marker_power = float(profile[inner].mean())
    # Mean power over the settled antenna dwells, using the found phase.
    ant = []
    for i in range(len(ANTENNAS)):
        s = (phase + sched.antenna_offset(i) + settle) % p
        idx = (s + np.arange(max(sched.dwell - settle, 1))) % p
        ant.append(profile[idx].mean())
    antenna_power = float(np.max(ant))

    contrast = 10 * math.log10(max(antenna_power, 1e-20) / max(marker_power, 1e-20))
    return PhaseEstimate(phase=phase, contrast_db=contrast, ambiguous=ambiguous)


# --------------------------------------------------------------------------- #
# Demultiplexing
# --------------------------------------------------------------------------- #
@dataclass
class Cycle:
    index: int
    start_sample: int  # absolute sample index of the N dwell
    power_db: dict[str, float]
    bearing_deg: float
    samples: dict[str, np.ndarray] = field(default_factory=dict, repr=False)


def bearing_from_powers(power_lin: dict[str, float]) -> float:
    """Amplitude-comparison bearing for N/E/S/W directional antennas.

    With cardioid-like patterns, (aN - aS) ~ cos(theta) and (aE - aW) ~
    sin(theta), so theta = atan2(aE - aW, aN - aS). 0 deg = N, 90 deg = E.
    """
    a = {k: math.sqrt(max(v, 0.0)) for k, v in power_lin.items()}
    return math.degrees(math.atan2(a["E"] - a["W"], a["N"] - a["S"])) % 360.0


def demux(
    chunks: Iterator[np.ndarray],
    sched: Schedule,
    settle: int,
    sync_cycles: int = 20,
    resync: bool = True,
    keep_samples: bool = False,
    resync_tolerance: int = 16,
    expected_phase: int = 0,
    log=sys.stderr,
) -> Iterator[Cycle]:
    """Yield one Cycle per complete N/E/S/W/marker rotation.

    expected_phase: absolute sample index (mod period) where an N dwell is
    expected. The firmware restarts the rotation at N when streaming starts,
    so 0 plus a fixed, calibratable startup offset. It is used only to break
    ties when an antenna null sits next to the marker.
    """
    p = sched.period
    need_sync = sync_cycles * p

    buf = np.empty(0, dtype=np.complex64)
    buf_start = 0  # absolute index of buf[0]
    phase: int | None = None
    next_cycle = 0  # absolute index of the next N dwell to emit
    n = 0
    last_msg = ""

    def say(msg: str) -> None:
        nonlocal last_msg
        if msg != last_msg:
            print(msg, file=log)
            last_msg = msg

    for chunk in chunks:
        buf = np.concatenate([buf, chunk])

        # Initial sync, or re-sync once per chunk to catch dropped samples.
        if len(buf) >= need_sync and (phase is None or resync):
            window_start = buf_start + len(buf) - need_sync
            expected = phase if phase is not None else expected_phase
            prior = (expected - window_start) % p
            window = buf[-need_sync:]
            half = (sync_cycles // 2) * p
            first = estimate_phase(window[:half], sched, settle, prior)
            second = estimate_phase(window[half:], sched, settle, (prior - half) % p)
            second_phase = (second.phase + half) % p
            agree = min((first.phase - second_phase) % p,
                        (second_phase - first.phase) % p) <= resync_tolerance
            est = first
            if not (first.reliable and second.reliable):
                if phase is None:
                    say("sync: weak marker contrast, waiting for signal")
            elif not agree:
                say("sync: window straddles a phase change (dropped samples?), waiting")
            else:
                new_phase = (window_start + est.phase) % p
                note = " (ambiguous: resolved with prior)" if est.ambiguous else ""
                if phase is None:
                    say(f"sync: phase={new_phase} contrast={est.contrast_db:.1f} dB{note}")
                    next_cycle = buf_start + ((new_phase - buf_start) % p)
                elif min((new_phase - phase) % p, (phase - new_phase) % p) > resync_tolerance:
                    say(f"resync: phase {phase} -> {new_phase} (samples dropped?) "
                        f"contrast={est.contrast_db:.1f} dB{note}")
                    next_cycle += (new_phase - (next_cycle % p)) % p
                phase = new_phase

        if phase is None:
            # Keep only enough history to sync on.
            if len(buf) > need_sync:
                drop = len(buf) - need_sync
                buf = buf[drop:]
                buf_start += drop
            continue

        # Emit every complete cycle in the buffer.
        while next_cycle + p <= buf_start + len(buf):
            base = next_cycle - buf_start
            powers, slices = {}, {}
            for i, name in enumerate(ANTENNAS):
                s = base + sched.antenna_offset(i) + settle
                e = base + sched.antenna_offset(i) + sched.dwell
                seg = buf[s:e]
                powers[name] = float(np.mean(np.abs(seg) ** 2)) if seg.size else 0.0
                if keep_samples:
                    slices[name] = seg.copy()
            yield Cycle(
                index=n,
                start_sample=next_cycle,
                power_db={k: 10 * math.log10(max(v, 1e-20)) for k, v in powers.items()},
                bearing_deg=bearing_from_powers(powers),
                samples=slices,
            )
            n += 1
            next_cycle += p

        # Trim consumed samples, keeping sync history.
        keep_from = min(next_cycle - buf_start, max(len(buf) - need_sync, 0))
        if keep_from > 0:
            buf = buf[keep_from:]
            buf_start += keep_from


# --------------------------------------------------------------------------- #
# Self-test with a simulated capture
# --------------------------------------------------------------------------- #
def simulate(sched: Schedule, cycles: int, bearing_deg: float, phase: int,
             snr_db: float = 20.0, seed: int = 1) -> np.ndarray:
    """Build a switched capture: CW emitter at bearing_deg, cardioid antennas."""
    rng = np.random.default_rng(seed)
    p = sched.period
    total = cycles * p + phase
    t = np.arange(total)
    tone = 0.3 * np.exp(2j * np.pi * 0.01 * t)
    noise_amp = 0.3 / (10 ** (snr_db / 20)) / math.sqrt(2)
    noise = noise_amp * (rng.standard_normal(total) + 1j * rng.standard_normal(total))

    gain = np.zeros(total)
    theta = math.radians(bearing_deg)
    pos = (t - phase) % p
    for i, point in enumerate((0.0, 90.0, 180.0, 270.0)):
        g = 0.5 * (1 + math.cos(theta - math.radians(point)))
        s = sched.antenna_offset(i)
        gain[(pos >= s) & (pos < s + sched.dwell)] = g
    # Marker port: terminated, so the gain stays 0 there (noise only).
    return (gain * tone + noise).astype(np.complex64)


def selftest() -> int:
    sched = Schedule(dwell=20000, marker=2000)
    settle = default_settle(20e6)
    ok = True
    for bearing, phase in [(0, 0), (45, 500), (60, 12345), (90, 40000), (135, 81999),
                           (180, 7), (200, 70001), (270, 61000), (315, 3)]:
        iq = simulate(sched, cycles=120, bearing_deg=bearing, phase=phase)
        chunks = (iq[i : i + 262144] for i in range(0, iq.size, 262144))
        res = list(demux(chunks, sched, settle, log=io.StringIO(),
                         expected_phase=phase))
        if not res:
            print(f"FAIL bearing={bearing}: no cycles")
            ok = False
            continue
        est = float(np.median([c.bearing_deg for c in res]))
        err = (est - bearing + 180) % 360 - 180
        start_ok = res[0].start_sample % sched.period == phase % sched.period
        status = "ok" if abs(err) < 3 and start_ok else "FAIL"
        ok &= status == "ok"
        print(f"{status}: true={bearing:>3} deg  est={est:6.1f} deg  "
              f"cycles={len(res)}  phase_ok={start_ok}")
    return 0 if ok else 1


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("capture", nargs="?", help="hackrf_transfer int8 I/Q file")
    ap.add_argument("--fs", type=float, default=20e6, help="sample rate (Hz)")
    ap.add_argument("--dwell", type=int, default=20000, help="samples per antenna")
    ap.add_argument("--marker", type=int, default=2000, help="samples on marker port")
    ap.add_argument("--settle", type=int, default=None,
                    help="samples dropped after each switch (default: auto)")
    ap.add_argument("--sync-cycles", type=int, default=20,
                    help="cycles folded to find the marker")
    ap.add_argument("--no-resync", action="store_true",
                    help="lock phase after the first sync")
    ap.add_argument("--expected-phase", type=int, default=0,
                    help="calibrated sample offset of the first N dwell "
                         "(breaks ties when an antenna null sits next to the marker)")
    ap.add_argument("--csv", help="write per-cycle results to this CSV")
    ap.add_argument("--selftest", action="store_true", help="run simulated test")
    args = ap.parse_args()

    if args.selftest:
        return selftest()
    if not args.capture:
        ap.error("capture file required (or use --selftest)")

    sched = Schedule(dwell=args.dwell, marker=args.marker)
    settle = args.settle if args.settle is not None else default_settle(args.fs)
    if settle * 2 >= min(sched.dwell, sched.marker):
        ap.error("settle too long for the dwell/marker lengths")

    chunks = read_iq_int8(args.capture, chunk_samples=max(sched.period * 20, 1 << 20))
    writer, fh = None, None
    if args.csv:
        fh = open(args.csv, "w", newline="")
        writer = csv.writer(fh)
        writer.writerow(["cycle", "start_sample", "time_s",
                         *[f"{a}_dB" for a in ANTENNAS], "bearing_deg"])

    bearings = []
    for c in demux(chunks, sched, settle, args.sync_cycles, not args.no_resync,
                   expected_phase=args.expected_phase):
        bearings.append(c.bearing_deg)
        if writer:
            writer.writerow([c.index, c.start_sample, f"{c.start_sample / args.fs:.6f}",
                             *[f"{c.power_db[a]:.2f}" for a in ANTENNAS],
                             f"{c.bearing_deg:.1f}"])
    if fh:
        fh.close()

    if not bearings:
        print("no cycles decoded (no sync; is a signal present?)", file=sys.stderr)
        return 1
    b = np.radians(bearings)
    mean = math.degrees(math.atan2(np.sin(b).mean(), np.cos(b).mean())) % 360
    print(f"cycles={len(bearings)}  circular mean bearing={mean:.1f} deg "
          f"(settle={settle} samples, period={sched.period})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
