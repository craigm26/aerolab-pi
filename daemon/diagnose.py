"""Diagnosis: turn every check into "what's wrong, how we know, what to do".

Rules run on the live state, the self-test, the sensor monitors, the data
writers and the calibration. Each finding says:
  severity  - blocker (data would be lost or wrong), warn (degraded), info
  title     - one line
  evidence  - the numbers that triggered it
  steps     - what to do, most likely fix first; device-specific steps come
              from `troubleshooting:` in config/hardware.yaml

readiness() rolls them up into one verdict for the cockpit: is the science
data going to be recorded, and how good will it be.
"""

from __future__ import annotations

import os
from pathlib import Path

SERIAL_STEPS = [
    "Check the sensor has power (LED, or its supply voltage at the connector).",
    "Check the port: `ls -l /dev/serial/by-id/` should list the adapter; pick it again in ./configure.sh if it moved.",
    "Check the baud rate and protocol: stop the service (`sudo systemctl stop aerolab`) and run "
    "`python scripts/probe.py <port>`; it tries every baud and protocol and says what it hears.",
    "On a 3.3 V UART, swap TX and RX if nothing at all arrives. Never wire an RS-232 device to the Pi's pins: "
    "it needs a USB-RS232 adapter.",
]


def _dev(cfg: dict, section: str, catalog: dict | None) -> dict:
    c = cfg.get(section) or {}
    if not catalog or not c.get("device"):
        return {}
    for kind in ("lasers", "gnss", "attitude"):
        for d in catalog.get(kind, []):
            if d["id"] == c["device"]:
                return d
    return {}


def _ts(dev: dict, key: str) -> list[str]:
    return list((dev.get("troubleshooting") or {}).get(key, []))


def findings(d, catalog: dict | None = None) -> list[dict]:
    """d is the AeroLabDaemon."""
    s, cfg, out = d.state, d.cfg, []
    names = d.buddy.names()
    conf = d.buddy.configured
    health = d.health()

    def add(sev, title, evidence, steps):
        out.append({"severity": sev, "title": title, "evidence": evidence, "steps": steps})

    # ---- data safety
    data = d.data_status()
    if not data["db_ok"] or data["queued"] > 2000:
        add("blocker", "Science data is not reaching the disk",
            f"{data['queued']} readings waiting, database writable: {data['db_ok']}",
            ["Check the log disk is plugged in and mounted: `lsblk`, `mount | grep ssd`.",
             "Check free space: `df -h` on the log disk.",
             "Readings wait in memory (about an hour's worth) and are written once the disk is back."])
    if data["dropped"]:
        add("warn", f"{data['dropped']} readings were lost while the disk was unavailable",
            "the memory queue overflowed", ["Find out why the disk went away (cable, power, mount) before the next flight."])
    for sec, w in data["raw"].items():
        if w["errors"]:
            add("warn", f"Raw capture for {sec} had {w['errors']} write errors", ", ".join(w["dirs"]),
                ["Check each log directory is mounted and writable.", "A failing USB stick or SSD shows up here first."])
    usable, missing = d.usable_mirrors()
    for m in missing:
        add("warn", f"Backup drive not mounted: {m}", "it is on the SD card's filesystem, so nothing is written there",
            ["Plug in the backup drive and check it mounts (`lsblk`, `/etc/fstab` with `nofail`).",
             "Until it does, there is one copy of the data, on the log disk."])
    if d.fallback_logging and not s.sim_mode:
        add("warn", "Logging to the SD card, not the SSD", f"log dir {d.log_dir}",
            ["Plug in the SSD and check it mounts at the configured path (`/etc/fstab` with `nofail`).",
             "With the read-only overlay on, SD-card logs are lost at power-off."])
    free = (d.buddy._slow or {}).get("disk_free_gb")
    if free is not None and free < 2:
        add("blocker" if free < 0.5 else "warn", "Log disk nearly full", f"{free:.1f} GB free",
            ["Copy off and remove old flights from the log disk.", "A full disk stops logging."])

    # ---- sensors
    for role, sec in (("gnss", "gnss"), ("laser", "laser_altimeter"), ("imu", "imu_ahrs")):
        if not conf.get(role):
            continue
        h = health[role]
        dev = _dev(cfg, sec, catalog)
        nm = names[role]
        port = (cfg.get(sec) or {}).get("port", "?")
        if h["age_ms"] is None or h["age_ms"] > 2000:
            sev = "blocker" if role in ("laser", "gnss") else "warn"
            add(sev, f"{nm}: no data", f"port {port}, last reading "
                + ("never" if h["age_ms"] is None else f"{h['age_ms'] / 1000:.0f} s ago"),
                _ts(dev, "no_data") + SERIAL_STEPS)
            continue
        if h["err_pct"] > 1.0:
            add("warn", f"{nm}: {h['err_pct']:.0f}% bad frames", f"checksums failing on {port}",
                ["A few percent usually means electrical noise or a marginal baud rate: shorten or shield the cable, "
                 "join grounds, keep it away from the alternator/ignition leads.",
                 "Run `python scripts/probe.py <port>` to confirm the baud."] + _ts(dev, "bad_frames"))
        if h["expected_hz"] and h["hz"] < 0.5 * h["expected_hz"]:
            add("warn", f"{nm}: data rate {h['hz']:g} Hz, expected {h['expected_hz']:g}", f"port {port}",
                _ts(dev, "low_rate") + ["Check the device's output rate setting.",
                                        "At low baud rates the link itself can be the limit."])

    # ---- laser specifics
    if conf.get("laser") and getattr(s, "laser_alive", False) and s.laser_no_return_pct > 20:
        dev = _dev(cfg, "laser_altimeter", catalog)
        add("warn" if s.laser_ok else "blocker",
            f"Laser getting no ground return {s.laser_no_return_pct:.0f}% of the time",
            "the laser is talking (cable and power are fine) but reports no target",
            _ts(dev, "range") + _ts(dev, "spikes") +
            ["On the ramp: check nothing blocks the beam and the laser points straight down.",
             "In flight: usually height beyond the laser's range, a dark or wet surface, or cloud below."])
    if conf.get("laser") and s.laser_ok:
        dev = _dev(cfg, "laser_altimeter", catalog)
        gate = d.laser_gate
        if gate.spikes > 50:
            add("info", f"Laser spike filter rejected {gate.spikes} readings this session", "",
                ["Normal over broken ground, water or birds. If it climbs fast in clean conditions, check the laser "
                 "window for ice, dirt or condensation."] + _ts(dev, "spikes"))
        rng = float((cfg.get("laser_altimeter") or {}).get("max_range_m") or 0)
        survey = float((cfg.get("hud") or {}).get("survey_agl_ft") or 0) / 3.28084
        if rng and survey > rng:
            add("warn", "Planned survey height is beyond the laser's rated range",
                f"survey {survey:.0f} m, laser rated {rng:.0f} m",
                ["Plan legs below the laser's range, or expect no laser data at survey height."] + _ts(dev, "range"))
    if conf.get("laser") and not conf.get("imu") and not s.sim_mode:
        add("info", "AGL on the display is not tilt-corrected", "no attitude source configured",
            ["Fine when attitude is applied in post-processing. In a bank the display reads high by 1/cos(bank) "
             "(about 6% at 20°)."])

    # ---- GNSS specifics
    if conf.get("gnss") and s.gnss_ok:
        dev = _dev(cfg, "gnss", catalog)
        if s.gps_fix == 0:
            add("blocker", "GNSS has no position fix", f"{max(s.sats, 0)} satellites",
                _ts(dev, "no_fix") + ["Give the antenna a clear view of the sky; inside a hangar it won't fix.",
                                      "Check the antenna cable and connector.",
                                      "A cold start can take a few minutes."])
        elif 0 <= s.sats < 6 or s.hdop > 2.5:
            add("warn", "Weak GNSS geometry", f"{s.sats} satellites, HDOP {s.hdop:.1f}",
                ["Wait for more satellites before take-off.", "Check nothing metal shadows the antenna."])
    if s.gnss_time_offset_s is not None and abs(s.gnss_time_offset_s) > 1.0:
        add("warn", "Pi clock disagrees with GNSS time", f"{s.gnss_time_offset_s:+.1f} s",
            ["Every reading is also stamped with the monotonic clock and GNSS epoch time, so "
             "tools/export_laser.py still puts it on GNSS time. Nothing is lost.",
             "To fix the clock itself: connect to the internet once, or fit the Pi 5 RTC battery."])

    # ---- Reach raw log
    reach = getattr(d, "reach", None)
    if reach is not None and reach.enabled:
        rst = reach.status()
        if rst.get("reachable"):
            if rst.get("writing") is False:
                sv = getattr(d, "survey", None)
                recording = sv is not None and getattr(sv, "state", "") == "recording"
                add("blocker" if recording else "warn",
                    "Reach raw log is not recording",
                    f"Reach at {reach.url} reports is_writing false" if reach.url else "Reach reports is_writing false",
                    ["Turn on Autostart in Emlid Flow: Receivers > Logging > Recorded logs and settings > gear > Autostart.",
                     "Check free storage on the Reach; a full memory stops logging.",
                     "Check the receiver is not in base mode."])
        elif reach.url:
            add("info",
                "Reach raw log status not reachable",
                f"{reach.url}" + (f" ({rst['error']})" if rst.get("error") else ""),
                ["Check the Pi and the Reach are on the same network (or connected to the Reach hotspot).",
                 "Check the Reach IP address in config (gnss.reach_url) matches the receiver.",
                 f"Confirm Reach Panel opens in a browser at {reach.url}."])

    # ---- camera that captures on its own
    cam = getattr(d, "camera", None)
    if cam is not None and cam.enabled:
        c = cam.snapshot()
        if not c["gpio_ok"]:
            add("warn", f"{c['name']}: exposure input unavailable", c["gpio_error"] or f"GPIO {c['pin']}",
                ["Check no other program uses that GPIO and the user is in the gpio group (reboot after install)."])
        elif not c["exposures"] or c["stopped"]:
            add("warn", f"{c['name']}: no exposures " + ("seen" if not c["exposures"] else f"for {c['age_s']:.0f} s"),
                f"watching GPIO {c['pin']} (falling edge)",
                ["Check the camera is powered and its auto-capture is on: web page → Settings → Auto capture mode "
                 "'timer', with the period you want.",
                 "Check a camera pin is set to Top of Frame Output and wired to the Reach time mark AND to Pi "
                 f"GPIO {c['pin']}, grounds joined.",
                 "Images are still being stored by the camera if it is capturing; this only means the Pi can't see "
                 "the exposures."])
        if c["status_url"] and not c["status_ok"] and c["status_age_s"] is None:
            add("info", f"{c['name']}: status not reachable", c["status_url"],
                ["Connect the camera's Ethernet to the Pi; give the Pi's Ethernet a static address on 192.168.1.x "
                 "(the camera is 192.168.1.83).", "Optional: exposures are monitored without it."])
        st = c["status"]
        if c["status_ok"] and st.get("auto_cap_active") is False:
            add("warn", f"{c['name']}: auto-capture is off", "camera /status says auto_cap_active false",
                ["Turn on auto capture (timer) in the camera's web page, or the camera will not take pictures."])
        if c["status_ok"] and isinstance(st.get("sd_gb_free"), (int, float)) and st["sd_gb_free"] < 5:
            add("warn", f"{c['name']}: SD card low", f"{st['sd_gb_free']} GB free",
                ["Swap or empty the camera's card before the next flight."])

    # ---- timing (PPS)
    pps = getattr(d, "pps", None)
    if pps is not None and pps.enabled:
        st = pps.status()
        if not st["ok"]:
            add("warn", "No PPS pulses", st["detail"],
                ["Check the GNSS PPS output is wired to the configured GPIO and shares ground with the Pi.",
                 "Check `/boot/firmware/config.txt` has the pps-gpio overlay on that pin, then reboot.",
                 "Without PPS, timing comes from GNSS epoch times (milliseconds instead of microseconds)."])

    # ---- calibration
    if not d.cal.updated:
        add("warn", "Not calibrated", "no ground calibration saved",
            ["Buddy → Calibrate on the ramp before the first flight (30 s, engine off)."])
    cap = d.capture
    if cap and cap.done and cap.result["status"] == "fail":
        add("warn", "Last ground calibration failed", "; ".join(i["item"] for i in cap.result["items"] if i["status"] == "fail"),
            ["Fix the failed items (usually a sensor with no data) and run it again."])

    # ---- power and heat
    h = d.buddy._slow or {}
    if h.get("undervoltage_now"):
        add("blocker", "Pi undervoltage now", "the supply is sagging",
            ["Use a supply rated 5 V 5 A for a Pi 5 with USB devices, short thick cables, and a fused, isolated "
             "DC-DC converter from the aircraft or a dedicated battery.",
             "Undervoltage can corrupt the SD card and drop USB sensors."])
    elif h.get("undervoltage_ever"):
        add("warn", "Undervoltage happened since boot", "", ["Check the supply and cables before flight."])
    if h.get("cpu_c") and h["cpu_c"] >= 80:
        add("warn", "Pi running hot", f"{h['cpu_c']:.0f} °C",
            ["Add a heatsink and fan (the Pi 5 Active Cooler), shade it from the sun, give it airflow."])

    # ---- supervisor
    for name, st in d.sup.status().items():
        if st["restarts"]:
            add("info", f"{name} restarted {st['restarts']} time(s)", "it crashed and was restarted automatically",
                ["Logging continued. The service log has the details: `journalctl -u aerolab`."])
    order = {"blocker": 0, "warn": 1, "info": 2}
    return sorted(out, key=lambda f: order[f["severity"]])


def readiness(fs: list[dict]) -> dict:
    blockers = [f for f in fs if f["severity"] == "blocker"]
    warns = [f for f in fs if f["severity"] == "warn"]
    if blockers:
        return {"level": "red", "text": f"Not ready: {blockers[0]['title']}", "blockers": len(blockers), "warnings": len(warns)}
    if warns:
        return {"level": "amber", "text": f"Recording; {len(warns)} thing(s) to check", "blockers": 0, "warnings": len(warns)}
    return {"level": "green", "text": "Ready: every sensor is recording", "blockers": 0, "warnings": 0}


def as_text(fs: list[dict], limit: int = 3) -> str:
    if not fs:
        return "Nothing wrong that I can see: every sensor is recording and the data is reaching the disk."
    lines = []
    for f in fs[:limit]:
        lines.append(f"{f['title']}" + (f" ({f['evidence']})" if f["evidence"] else "") + ".")
        if f["steps"]:
            lines.append("  First: " + f["steps"][0])
    if len(fs) > limit:
        lines.append(f"…and {len(fs) - limit} more in Checks.")
    return "\n".join(lines)


def git_version(root: Path) -> str:
    head = root / ".git" / "HEAD"
    try:
        ref = head.read_text().strip()
        if ref.startswith("ref: "):
            return (root / ".git" / ref[5:]).read_text().strip()[:10]
        return ref[:10]
    except OSError:
        return os.environ.get("AEROLAB_VERSION", "unknown")
