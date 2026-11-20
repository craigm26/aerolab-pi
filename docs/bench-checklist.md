# Bench checklist: snow-survey installation

Work through this on the bench or the ramp, in order. Each step says what "good" looks like. When you're done, run `./bench.sh --laser-distance <metres> --camera-test` and send the two files it prints.

You need: the Pi with AeroLab-Pi installed (`./install.sh`), the Reach M2 and antenna with a sky view, the ILM-150-R on its 9-24 V supply, the FTDI US232R-100, the RedEdge-P, a multimeter, a tape measure, and one short jumper wire.

## 1. Laser serial link

**1a. Prove the adapter and cable first.** Disconnect the laser. Bridge pins 2 and 3 of the US232R-100's DB9 with the jumper.

```bash
sudo systemctl stop aerolab
python scripts/probe.py /dev/serial/by-id/usb-FTDI_US232R* --loopback
```

Good: `PASS: … bytes out and back intact`. Remove the jumper.

**1b. Find the laser's pins on the Souriau harness.** Power the laser (9-24 V dc), with the adapter unplugged. Measure each signal pin against the laser's ground with a multimeter on DC volts:

| Reading | Pin |
|---|---|
| about −5 to −12 V, steady | **laser TX** (data out) → adapter DB9 pin 2 |
| near 0 V | laser RX (data in) → adapter DB9 pin 3 (optional; the laser streams on its own) |
| 0 V, continuity to the supply negative | ground → adapter DB9 pin 5 |
| the 9-24 V supply | **never** to the adapter |

**1c. Hear the laser.**

```bash
python scripts/probe.py /dev/serial/by-id/usb-FTDI_US232R* --bauds 9600
```

Good: `Found it: ascii_number at 9600 baud`. The sample shows lines like `00012.34m`; `99999.99m` means it sees nothing (point it at a wall). Nothing at all: the TX pin is wrong, or swap pins 2 and 3.

**1d. Check the range against a tape measure.** Point the laser squarely at a flat wall 10-20 m away. Measure from the laser's **rearmost mounting hole** (the range reference, 40 mm behind the lens) to the wall.

```bash
sudo systemctl start aerolab
./bench.sh --laser-distance 12.40
```

Good: `laser_check` bias within ±0.15 m (the laser is rated 10 cm), noise under 0.10 m, no-return 0.

## 2. Reach M2

**2a. Settings in Emlid Flow:**
- **Position output:** Serial (UART), NMEA, 10 Hz, 115200 baud.
- **Raw data logging:** on, UBX or RINEX, 10 Hz, with **Autostart** enabled (`Receivers > Logging > Recorded logs and settings > gear > Autostart`). That way the Reach logs the moment the instruments are switched on. AeroLab-Pi cannot switch the Reach's internal logging on or off. Before cutting power to the Reach, stop logging in Emlid Flow (or let Autostart's shutdown finish) so the active log is compressed and complete.

**2b. Wiring.** Make these connections:
- Reach S1 pin 4 (TX) → Pi pin 10;
- Reach S1 pin 5 (RX) → Pi pin 8;
- S1 pin 1 → a Pi ground pin.

All of it is 3.3 V, so no adapter is needed.

**2c. Serial check.** In the cockpit display, the GNSS pill shows a satellite count, and Buddy → Checks shows the Reach at about 10 Hz with 0% bad frames.

**2d. Raw-log network check (optional).** If the Reach and Pi share a Wi-Fi network (or the Pi joins the Reach hotspot at `http://192.168.42.1`), verify the read-only local interface on the bench:

```bash
python tools/reach_discover.py http://<reach-ip>
```

Send the `reach_discovery_*.json` file it writes. Look for `active_logs seen: True` and `raw.is_writing: True` in the summary. If that works on your M2 firmware, enter `http://<reach-ip>` as the Reach address in `./configure.sh` (`gnss.reach_url`) so the boot self-test, survey confirmation, diagnosis and shutdown review check it automatically. Without a Reach network URL (or when unreachable), START SURVEY asks you to tick the manual confirmation box after checking Emlid Flow.

## 3. PPS timing

**3a. Wiring.** Reach C1 pin 3 (PPS) → Pi GPIO 18 (pin 12). Reach C1 pin 1 → a Pi ground. Answer yes to the PPS question in `./configure.sh`; the installer enables the kernel PPS driver. Reboot once.

**3b. Kernel check.**

```bash
ls /sys/class/pps/          # pps0 should be listed
sudo ppstest /dev/pps0      # one line a second, once the Reach has a fix
```

**3c. Chrony clock sync check** (when "Set the Pi's clock from GNSS (chrony)?" was answered yes in `./configure.sh`):

```bash
chronyc sources
chronyc -n tracking
```

Good:
- `chronyc sources` lists `GNSS` (and `PPS` when PPS is wired) with non-zero `Reach` (climbing toward `377`). With PPS wired, `#* PPS` is selected and `#? GNSS` (marked `noselect`) feeds it the whole second; without PPS, `#* GNSS` is selected.
- `chronyc -n tracking` shows `Stratum : 1`, `Reference ID` as `PPS` (or `GNSS`), `Leap status : Normal`, and `System time` offset within microseconds (PPS) or tens of milliseconds (GNSS only).

**3d. Timing validation.** Leave it running for 10 minutes with a fix, then:

```bash
python tools/timing_check.py
```

Good:
- **PPS present** and **PPS interval:** PASS, with jitter in the microseconds.
- **Missing pulses:** none.
- **NMEA latency:** a steady figure, usually tens of milliseconds.
- **Pi clock vs GPS:** a large offset here is fine. The export tools correct for it.

## 4. RedEdge-P exposures

**4a. Camera settings** (web page):
- **Auto capture:** timer, at your period (for example 1 s).
- **Pins:** set one pin to **Top of Frame Output**. It rests high and falls at each frame.

**4b. Wiring.** Connect the Top-of-Frame pin → Reach C1 pin 2 (time mark) **and** → Pi GPIO 27 (pin 13). Join the camera, Reach and Pi grounds. Optional: connect the camera Ethernet to the Pi Ethernet. The Pi needs a static address, 192.168.1.x; the camera is 192.168.1.83.

**4c. Wiring test.**

```bash
./bench.sh --camera-test
```

Good: `camera_test` shows `edges_seen: 1` or more, and "ok: the exposure signal reaches the Pi". With Ethernet it also reports `request_to_edge_ms`. Without Ethernet, press the camera's capture button during the 10-second listening window.

**4d. Cross-check against the Reach.** Run a 5-minute survey on the bench (step 6), with the camera on its timer. Then export the Reach's time marks with Emlid Studio as an events `.pos` file, and:

```bash
python tools/compare_timemarks.py --events reach_events.pos
```

Good: `every exposure is in both records`. The `offset_ms_mean` is the Pi's backup timestamp error, which you can feed back as `tools/export_exposures.py --latency-ms`.

## 5. Ground calibration

On the ramp, engine off, nobody moving: open Buddy → Calibrate and enter the field elevation. The laser offset it measures is from the laser's reference point to the ground. Apply it.

## 6. Survey dry run

1. Tap **SURVEY**, then START. The readiness check should be green or amber.
2. Within 15 s it should say "Confirmed: everything is recording", with ticks for the laser, Reach, camera and disk.
3. Let it run 5 minutes, then hold **STOP SURVEY**.
4. Good: "Saved: … verified on N of N drive(s)". The package folder holds `laser_points.csv`, `exposures.csv`, `aerolab_flight.db`, the raw captures, `readiness.html`, `survey.json` and `MANIFEST.sha256`.

## 7. Send the report

```bash
./bench.sh --laser-distance <metres> --camera-test
```

Send the `bench_report_*.html` and `.json` it prints, plus `flight_<n>_timing.json` from step 3 if you ran it separately.
