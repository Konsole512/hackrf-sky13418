# SKY13418 SP8T switch for HackRF Pro

Used for driving an aftermarket **SKY13418-485LF** SP8T RF switch (as found on the BG7TBL
"SKY13418 SP8T" demo board) from a **HackRF Pro** P20 expansion header, reusing
the Opera Cake time-mode SCTimer path in the firmware. Includes an analysis
script that splits a switched-antenna capture into per-antenna dwells and
estimates a bearing. 

Note: Reason for using the BG7TBL specifically, over the Skyworks, is due to the BG7TBL having built-in resistors arranged as three divider pairs for V1-V3.
<img width="632" height="813" alt="HackRF" src="https://github.com/user-attachments/assets/9add4b62-a080-4558-96af-ecca8403596e" />


## Contents

| File | Purpose |
|------|---------|
| `hackrf-sky13418-switch.patch` | Firmware patch that adds `SKY13418_SWITCH` support. Applies from the repo root; matches the flashed build (V1 on P20 pin 11). |
| `sky_demux.py` | Post-processing tool. Demultiplexes an N/E/S/W + marker capture and reports per-antenna power and an amplitude-comparison bearing. |

---

## 1. Build the firmware

### Toolchain (macOS)

```bash
brew install cmake dfu-util
brew install --cask gcc-arm-embedded    # provides arm-none-eabi-gcc
```

Verified with `arm-none-eabi-gcc` 15.3.1 and `cmake` 4.4.3.

### Apply the patch and build

Run from the **repository root** (the patch paths are relative to it):

```bash
# 1. Apply the SKY13418 patch
git apply sky13418/hackrf-sky13418-switch.patch

# 2. Fetch the firmware's libopencm3 submodule (once)
git submodule update --init firmware/libopencm3

# 3. Build libopencm3 for the LPC43xx targets (once)
make -C firmware/libopencm3 TARGETS='lpc43xx/m4 lpc43xx/m0' -j8

# 4. Configure + build the HackRF Pro firmware with the switch option
cmake -S firmware -B firmware/build -DBOARD=PRALINE -DSKY13418_SWITCH=ON
make -C firmware/build -j8
```

Output: `firmware/build/hackrf_usb/hackrf_usb.bin` (plus a `.dfu` for recovery).

Notes:
- `BOARD=PRALINE` is the HackRF Pro (Board ID 5). The prebuilt Praline FPGA
  bitstream is already in-tree, so no FPGA toolchain is needed.
- `-DSKY13418_SWITCH=ON` is what activates this patch's code paths. Without it
  the firmware builds as a stock Opera Cake driver.

### Flash

With the HackRF Pro connected over USB:

```bash
hackrf_spiflash -w firmware/build/hackrf_usb/hackrf_usb.bin
hackrf_spiflash -R                 # reset into the new firmware
hackrf_info | grep Firmware        # confirm the new version string
```

---

## 2. Wiring: P20 header → SKY13418

Black = the demo board's `GND`/`VCC` header; control lines land on the board's
`V1`/`V2`/`V3` pins **through its onboard resistor dividers** (do not bypass
them - see the voltage note below).

| P20 pin | HackRF Pro signal | LPC4320 pin | SCTimer output | → SKY13418 | Notes |
|--------:|-------------------|-------------|----------------|------------|-------|
| 3  | 3V3AUX | — | — | **VDD** | Firmware enables this aux rail at boot; ~3.3 V |
| 11 | GPIO3_14 | P7_6 | CTOUT_11 | **V1** (MSB) | Through demo-board divider (~1.8 V high) |
| 10 | GPIO3_13 | P7_5 | CTOUT_12 | **V2**       | Through demo-board divider |
| 9  | GPIO3_12 | P7_4 | CTOUT_13 | **V3** (LSB) | Through demo-board divider |
| 13 | GND | — | — | **GND** | |
| —  | (RF) | — | — | **ANT / RFC** | Coax to the HackRF antenna jack (SMA) |

**Control-voltage note.** The SKY13418 control inputs are **1.8 V logic**: valid
high is 1.35–2.70 V and the absolute maximum is **3.0 V**. They must **not** be
driven directly by the 3.3 V P20 pins — the demo board's `R1/R4`, `R2/R5`,
`R3/R6` dividers bring each line to ~1.8 V. VDD, by contrast, is fine on the raw
3.3 V rail (its range is 2.5–4.8 V).

**About pin 11.** V1 is driven on **P20 pin 11 (CTOUT_11)** in this build. The
original design used pin 5 (CTOUT_14); it was remapped to pin 11. To move it back
to pin 5, change the three `CTOUT_11` references and the `bit2 << 11` output shift
back to `CTOUT_14` / `bit2 << 14` in `firmware/common/operacake_sctimer.c`, then
rebuild and move the V1 wire to pin 5.

---

## 3. Port → RF → antenna map

Opera Cake port index maps 1:1 onto the SKY13418 truth table. V1 is the MSB,
V3 the LSB (`V1 V2 V3`):

| Opera Cake port | V1 V2 V3 | SKY RF port | Intended use |
|-----------------|:--------:|:-----------:|--------------|
| A1 | 0 0 0 | RF1 | **North** antenna |
| A2 | 0 0 1 | RF2 | **East** antenna |
| A3 | 0 1 0 | RF3 | **South** antenna |
| A4 | 0 1 1 | RF4 | **West** antenna |
| B1 | 1 0 0 | RF5 | **50 Ω terminator** (sync marker) |
| B2 | 1 0 1 | RF6 | spare / calibration |
| B3 | 1 1 0 | RF7 | unused (terminate) |
| B4 | 1 1 1 | RF8 | unused (terminate) |

The switch is presented to the host as **Opera Cake at address 0**. In manual
mode, `hackrf_operacake` requires the A and B ports to be on opposite sides, so
pass a throwaway opposite port, e.g. select RF2 with `-a A2 -b B1`.

Quick per-port test (parks the switch on one port):

```bash
hackrf_operacake -o 0 -m manual -a A1 -b B1   # select RF1 (North)
hackrf_operacake -o 0 -m manual -a B1 -b A1   # select RF5 (marker)
```

---

## 4. Capture and compute a bearing (`sky_demux.py`)

The firmware cycles the switch through N, E, S, W and the marker in step with the
sample clock. Set up the rotation, then stream a capture:

```bash
# Dwell 20000 samples on each of N/E/S/W, 2000 on the 50-ohm marker
hackrf_operacake -o 0 -m time \
    -t A1:20000 -t A2:20000 -t A3:20000 -t A4:20000 -t B1:2000

# Capture (time-mode rotation only advances while streaming)
hackrf_transfer -r capture.iq -f 2440000000 -s 20000000
```

Process the capture (needs `numpy`):

```bash
# With uv (no venv needed):
uv run --with numpy python sky13418/sky_demux.py capture.iq --csv bearings.csv

# Or with a plain interpreter that has numpy:
python3 sky13418/sky_demux.py capture.iq --csv bearings.csv
```

Output: a per-cycle CSV (`cycle, start_sample, time_s, N_dB, E_dB, S_dB, W_dB,
bearing_deg`) and a summary line with the circular-mean bearing. Bearings use
**0° = North, 90° = East**, from an amplitude comparison of the four directional
antennas; the marker port is 50 Ω terminated so the demux can lock phase on the
quietest dwell.

Useful flags (defaults match the capture command above):

| Flag | Default | Meaning |
|------|---------|---------|
| `--fs` | `20e6` | Sample rate (Hz) |
| `--dwell` | `20000` | Samples per antenna (must match the `-t` args) |
| `--marker` | `2000` | Samples on the marker port |
| `--settle` | auto | Samples dropped after each switch (default ≈5 µs) |
| `--sync-cycles` | `20` | Cycles folded to find the marker |
| `--no-resync` | off | Lock phase after the first sync |
| `--expected-phase` | `0` | Calibrated offset of the first N dwell (tie-breaker) |
| `--csv PATH` | — | Write per-cycle results |
| `--selftest` | — | Run the built-in simulated test (no hardware) |

Sanity-check the script without hardware:

```bash
uv run --with numpy python sky13418/sky_demux.py --selftest
```

> The bearing is a starting point: it assumes four matched directional antennas
> pointing N/E/S/W and needs calibration against a known source on real hardware.

---

## 5. Troubleshooting

- **A control line never switches the RF (one bit ignored).** Park the switch so
  that line is high (e.g. `-a B1 -b A1` drives V1 high) and check ~1.8 V at the
  divider output *and* at the switch pin. A dead jumper wire or a cracked joint
  on the control line is the usual cause; the demo board's shunt resistor pulls
  an open line to 0 V, so the switch reads that bit as permanently low.
- **No sync / "weak marker contrast".** There must be a real signal present and
  the `--dwell`/`--marker` values must match the `hackrf_operacake -t` plan. The
  rotation only runs while `hackrf_transfer` is streaming.
- **Rebuilding from scratch.** Re-apply the patch on a clean tree and repeat
  section 1. The firmware customization lives only in this patch, so keep it.
