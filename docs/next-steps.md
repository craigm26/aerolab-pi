# Next steps before hardware testing

Work for the snow-survey installation. Items 1-4 and GNSS/PPS system clock sync via chrony (`daemon/timesync.py`, `install.sh`) are built and tested in simulation; what still needs the real hardware is marked **On the bench**.

## 1. Readiness check: is the Reach M2's own raw log running? (built)

Built: `daemon/reach.py` reads the Reach's live `active_logs` status (read-only) over its local web interface, as documented by the open-source home-assistant-emlid project. It shows the result in four places: the self-test, the survey confirmation, Buddy's Checks, and shutdown. When the Reach can't be reached, START asks for a manual tick instead. **On the bench:** run `python tools/reach_discover.py http://<reach-ip>` once, because that interface was verified on Reach RS3/RS4 firmware 35, not yet on an M2.

Original plan:

Emlid doesn't document a way to ask the Reach whether it is logging. Emlid Flow talks to the Reach over a private local API that can change between firmware versions. So, in three layers:

1. **Research.** Look for a stable, known way to read the logging status from the Reach on the local network.
2. **Discovery tool.** `tools/reach_discover.py` asks the Reach a short list of status questions over the local network, read-only, and saves the answers. Run it on the bench with the Reach on the Pi's network, so support is built against the real firmware rather than a guess.
3. **Fallback that always works.** START SURVEY gets a required tick: "Reach raw logging confirmed in Emlid Flow". It is recorded with the survey. If step 2 finds a usable status, the tick becomes an automatic check, and the manual tick stays as a backup.

## 2. Shutdown blocks on problems (built)

Shutdown becomes two phases:

- **Prepare:**
  1. Stop any survey.
  2. Copy the latest flight database and raw captures to every backup drive and verify them. This happens even with no survey running, so transit legs are covered.
  3. Ask the camera to park its card, and wait for it to confirm.
- **Review:** if everything passed, hold to power off. If anything failed, it stops and names the failure, for example "Backup to /mnt/usb2 failed: not mounted" or "Camera did not confirm its card is parked". Options: Retry, Shut down anyway (a longer hold, logged), or Cancel.

## 3. Camera images (built)

The RedEdge-P keeps its images on its own card, and they are backed up separately.

- **(a) Image list** (built): at STOP, the Pi saves the camera's file list (name, size, time, via the documented `/files` API) into the survey package. A check tool compares a separate backup of the card against that list, so a missing or truncated image shows up.
- **(b) Image copy** (built): `daemon/camera_copy.py` copies the camera's `/files` tree over Ethernet into `<log_dir>/camera_images/<YYYYMMDD_HHMMSS>/` after landing, triggered from **COPY CAMERA IMAGES TO PI** in the survey sheet. It streams files to `.part` before renaming on exact size match, retries mismatches up to 3 times, skips already-completed files on resume, writes `MANIFEST.sha256` and `copy_report.json`, checks for 2 GB of reserve disk space, and refuses or pauses automatically while a survey is running or the aircraft is moving. **On the bench:** test copy throughput and resume against the real RedEdge-P over Ethernet.

## 4. Install test before the first real install (built: dry run)

Built: `./install.sh --dry-run [--defaults|--sim]` prints every system change without making it. The first real install is still the first test on a clean Raspberry Pi OS image.

`install.sh` changes system settings (serial console, sudoers, udev, boot overlays) and hasn't yet run on a clean Raspberry Pi OS image. **Decision:**
- **Full test:** run the whole install plus the simulator on a freshly flashed spare Pi 5 / SD card;
- **Without a spare:** add `./install.sh --dry-run`, which prints every system change without making it, plus a shell-script lint, and treat the first bench install as the test.

## 5. First bench session

Follow [bench-checklist.md](bench-checklist.md) in this order:
1. **Laser serial link:** adapter loopback, Souriau pins, probe, tape-measure check.
2. **Reach M2:** NMEA output, raw logging set to start on power-up.
3. **PPS timing:** `tools/timing_check.py`.
4. **Camera exposures:** wiring test, then Reach time marks against the Pi's exposure list.
5. **Ground calibration.**
6. **A 5-minute survey dry run.**

Send the `./bench.sh` report files and the timing check.

At 450 ft the ILM-150-R is near its rated range (137 m of 150 m), so watch the no-return rate on the first flights.
