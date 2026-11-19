"""Calibration: measured corrections for this installation, and how to measure them.

Saved in config/calibration.local.json (git-ignored) and applied from then on:

  laser_offset_m     vertical distance laser -> wheels/belly, measured on the ground
                     (replaces the hand-entered mounting_offset_m)
  imu_roll_bias_deg  how far the attitude sensor reads off when the aircraft sits at
  imu_pitch_bias_deg the reference attitude you measured with an inclinometer
  boresight_roll_deg laser beam tilt relative to the aircraft, fitted from banks flown
  boresight_pitch_deg over flat ground (tools/boresight.py)
  laser_scale        range scale factor (1.0 unless calibrated against a surveyed height)

Ground calibration: aircraft parked on firm, level-ish ground, engine off, nobody
moving in the cabin, for 30 s. It also measures noise and GNSS jitter and grades
each one against limits, so a bad sensor shows up before flight.
"""

from __future__ import annotations

import json
import math
import statistics
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from integrity import haversine_m

# Pass limits for the ground capture. Engine off, aircraft still.
LIMITS = {
    "laser_std_m": 0.10,        # laser noise on a still target
    "imu_std_deg": 0.30,        # attitude noise, still
    "imu_bias_warn_deg": 5.0,   # bigger than this = sensor mounted crooked
    "gnss_jitter_m": 3.0,       # 2D position scatter, standalone GNSS
    "gnss_sats": 6,
    "gnss_hdop": 2.5,
    "field_elev_m": 15.0,       # |GNSS altitude - field elevation - antenna height|
    "clock_s": 1.0,
    "min_samples": 20,
}


@dataclass
class Calibration:
    laser_offset_m: float | None = None
    imu_roll_bias_deg: float = 0.0
    imu_pitch_bias_deg: float = 0.0
    boresight_roll_deg: float = 0.0
    boresight_pitch_deg: float = 0.0
    laser_scale: float = 1.0
    updated: str = ""
    history: list = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> "Calibration":
        try:
            d = json.loads(Path(path).read_text())
        except (OSError, ValueError):
            return cls()
        known = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        return cls(**known)

    def save(self, path: Path):
        self.updated = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        tmp = Path(path).with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2))
        tmp.replace(path)

    def correct_attitude(self, roll: float, pitch: float) -> tuple[float, float]:
        return roll - self.imu_roll_bias_deg, pitch - self.imu_pitch_bias_deg

    def summary(self) -> dict:
        d = asdict(self)
        d.pop("history", None)
        d["calibrated"] = bool(self.updated)
        return d


def _std(xs):
    return statistics.pstdev(xs) if len(xs) > 1 else 0.0


def robust(xs: list[float]) -> tuple[float, float, int]:
    """Median, robust standard deviation (1.4826 x MAD) and how many outliers were set aside.

    A few spikes (a bird, a person walking under the laser) must not move the
    offset or inflate the noise figure.
    """
    med = statistics.median(xs)
    mad = statistics.median(abs(x - med) for x in xs) * 1.4826
    lim = max(5 * mad, 1e-6)
    kept = [x for x in xs if abs(x - med) <= lim]
    return statistics.fmean(kept), (_std(kept) if len(kept) > 1 else 0.0), len(xs) - len(kept)


class GroundCapture:
    """Collect still samples for `seconds`, then grade them and propose corrections."""

    def __init__(self, seconds: float = 30.0, ref_roll: float = 0.0, ref_pitch: float = 0.0,
                 field_elev_m: float | None = None, antenna_height_m: float = 1.5):
        self.seconds = seconds
        self.ref_roll, self.ref_pitch = ref_roll, ref_pitch
        self.field_elev_m = field_elev_m
        self.antenna_height_m = antenna_height_m
        self.started = time.time()
        self.laser: list[float] = []
        self.roll: list[float] = []
        self.pitch: list[float] = []
        self.pos: list[tuple[float, float]] = []
        self.alt: list[float] = []
        self.sats: list[int] = []
        self.hdop: list[float] = []
        self.clock: list[float] = []
        self.trig0: int | None = None
        self.trig1: int | None = None
        self.result: dict | None = None

    @property
    def progress(self) -> float:
        return min(1.0, (time.time() - self.started) / self.seconds)

    @property
    def done(self) -> bool:
        return self.result is not None

    def add(self, *, laser_raw=None, roll_raw=None, pitch_raw=None, lat=None, lon=None, alt=None,
            fix=0, sats=None, hdop=None, clock_offset=None, trigger_count=None):
        if laser_raw is not None:
            self.laser.append(laser_raw)
        if roll_raw is not None and pitch_raw is not None:
            self.roll.append(roll_raw)
            self.pitch.append(pitch_raw)
        if fix and lat is not None:
            self.pos.append((lat, lon))
            if alt is not None:
                self.alt.append(alt)
        if sats is not None and sats >= 0:
            self.sats.append(sats)
        if hdop:
            self.hdop.append(hdop)
        if clock_offset is not None:
            self.clock.append(clock_offset)
        if trigger_count is not None:
            if self.trig0 is None:
                self.trig0 = trigger_count
            self.trig1 = trigger_count

    def finish(self, cal: Calibration, trigger_enabled: bool, configured: dict) -> dict:
        L = LIMITS
        items: list[dict] = []
        proposed: dict = {}

        def item(name, status, detail, **extra):
            items.append({"item": name, "status": status, "detail": detail, **extra})

        # Laser: on the ground true AGL is 0, so the vertical range IS the offset.
        if configured.get("laser", True):
            if len(self.laser) < L["min_samples"]:
                item("Laser", "fail", f"only {len(self.laser)} readings")
            else:
                mean, sd, outl = robust(self.laser)
                vert = (mean * cal.laser_scale * math.cos(math.radians(self.ref_roll + cal.boresight_roll_deg))
                        * math.cos(math.radians(self.ref_pitch + cal.boresight_pitch_deg)))
                proposed["laser_offset_m"] = round(vert, 3)
                st = "ok" if sd <= L["laser_std_m"] else "warn"
                item("Laser", st, f"{len(self.laser)} readings" + (f" ({outl} outliers set aside)" if outl else "")
                     + f", mean {mean:.3f} m, noise {sd * 100:.1f} cm "
                     f"(limit {L['laser_std_m'] * 100:.0f} cm). Offset {vert:.3f} m"
                     + (f", was {cal.laser_offset_m:.3f} m" if cal.laser_offset_m is not None else ""),
                     value=vert, std=sd)

        # Attitude: bias against the reference attitude measured with an inclinometer.
        if configured.get("imu", True):
            if len(self.roll) < L["min_samples"]:
                item("Attitude", "fail", f"only {len(self.roll)} readings")
            else:
                r, sr, _ = robust(self.roll)
                p, sp, _ = robust(self.pitch)
                br, bp = r - self.ref_roll, p - self.ref_pitch
                proposed["imu_roll_bias_deg"] = round(br, 3)
                proposed["imu_pitch_bias_deg"] = round(bp, 3)
                st = "ok"
                notes = []
                if max(sr, sp) > L["imu_std_deg"]:
                    st = "warn"
                    notes.append("noisy: is the engine off and the aircraft still?")
                if max(abs(br), abs(bp)) > L["imu_bias_warn_deg"]:
                    st = "warn"
                    notes.append("large bias: check how the sensor is mounted")
                item("Attitude", st, f"bias roll {br:+.2f}°, pitch {bp:+.2f}° against reference "
                     f"({self.ref_roll:+.1f}°, {self.ref_pitch:+.1f}°); noise {max(sr, sp):.2f}°"
                     + ("; " + "; ".join(notes) if notes else ""), roll_bias=br, pitch_bias=bp)

        # GNSS: fix, geometry, scatter of a stationary antenna.
        if configured.get("gnss", True):
            if len(self.pos) < 5:
                item("GNSS", "fail", "no position fix during the capture")
            else:
                lat0 = statistics.fmean(p[0] for p in self.pos)
                lon0 = statistics.fmean(p[1] for p in self.pos)
                d = [haversine_m(lat0, lon0, la, lo) for la, lo in self.pos]
                jitter = math.sqrt(statistics.fmean(x * x for x in d))
                sats = min(self.sats) if self.sats else -1
                hdop = max(self.hdop) if self.hdop else None
                st = "ok"
                bits = [f"scatter {jitter:.2f} m RMS (limit {L['gnss_jitter_m']:g} m)"]
                if jitter > L["gnss_jitter_m"]:
                    st = "warn"
                if sats >= 0:
                    bits.append(f"min {sats} satellites")
                    if sats < L["gnss_sats"]:
                        st = "warn"
                if hdop is not None:
                    bits.append(f"max HDOP {hdop:.1f}")
                    if hdop > L["gnss_hdop"]:
                        st = "warn"
                item("GNSS", st, ", ".join(bits), jitter=jitter)
                if self.field_elev_m is not None and self.alt:
                    alt = statistics.fmean(self.alt)
                    diff = alt - (self.field_elev_m + self.antenna_height_m)
                    item("GNSS altitude", "ok" if abs(diff) <= L["field_elev_m"] else "warn",
                         f"{alt:.1f} m vs field {self.field_elev_m:.1f} m + antenna {self.antenna_height_m:.1f} m: "
                         f"{diff:+.1f} m (limit ±{L['field_elev_m']:g} m; HAE sources read ~geoid height off)")

        if self.clock:
            off = statistics.fmean(self.clock)
            item("Clock vs GNSS", "ok" if abs(off) <= L["clock_s"] else "warn", f"{off:+.2f} s")

        if trigger_enabled:
            n = (self.trig1 or 0) - (self.trig0 or 0)
            item("Payload trigger", "ok" if n > 0 else "fail", f"{n} pulses during the capture")

        st = {i["status"] for i in items}
        self.result = {
            "status": "fail" if "fail" in st else ("warn" if "warn" in st else "ok"),
            "items": items, "proposed": proposed, "seconds": self.seconds,
            "reference": {"roll": self.ref_roll, "pitch": self.ref_pitch},
        }
        return self.result


def apply_proposed(cal: Calibration, proposed: dict, path: Path) -> Calibration:
    cal.history.append({"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "before": {k: getattr(cal, k) for k in proposed}, "after": proposed})
    cal.history = cal.history[-20:]
    for k, v in proposed.items():
        setattr(cal, k, v)
    cal.save(path)
    return cal


def fit_boresight(samples: list[tuple], offset_m: float,
                  grid_deg: float = 3.0, step_deg: float = 0.02) -> dict:
    """Fit laser boresight (roll, pitch) from flight over flat ground.

    samples: (range_m, roll_deg, pitch_deg) or (range_m, roll_deg, pitch_deg, ref_alt_m),
    over flat terrain, including banks both ways.

    With ref_alt_m (GNSS altitude), the laser height should track GNSS altitude
    minus a constant ground elevation, so we minimise the spread of
    (laser height - GNSS altitude). That works while the aircraft climbs or
    descends. Without it we assume constant height and minimise the relative
    spread (std / mean) of laser height; relative, because a plain spread would
    reward any error that shrinks every height.

    Pitch boresight only shows up if pitch itself varies; with less than 4 deg
    of pitch change it is not observable and is left at 0.
    """
    have_ref = all(len(s) >= 4 and s[3] is not None for s in samples)
    pitches = [s[2] for s in samples]
    rolls = [s[1] for s in samples]
    pitch_span = max(pitches) - min(pitches) if pitches else 0.0
    fit_pitch = pitch_span >= 4.0

    def heights(dr, dp):
        return [s[0] * math.cos(math.radians(s[1] + dr)) * math.cos(math.radians(s[2] + dp)) - offset_m
                for s in samples]

    def score(dr, dp):
        hs = heights(dr, dp)
        if have_ref:
            d = [h - s[3] for h, s in zip(hs, samples)]
            sd = statistics.pstdev(d)
            return sd, statistics.fmean(hs), sd
        m = statistics.fmean(hs)
        sd = statistics.pstdev(hs)
        return (sd / m if m > 0 else float("inf")), m, sd

    best = (float("inf"), 0.0, 0.0, 0.0, 0.0)
    step = 0.5
    lo_r, hi_r = -grid_deg, grid_deg
    lo_p, hi_p = (-grid_deg, grid_deg) if fit_pitch else (0.0, 0.0)
    while step >= step_deg:
        r = lo_r
        while r <= hi_r + 1e-9:
            p = lo_p
            while p <= hi_p + 1e-9:
                val, mean, sd = score(r, p)
                if val < best[0]:
                    best = (val, r, p, mean, sd)
                p += step
            r += step
        _, br, bp, _, _ = best
        lo_r, hi_r = br - step, br + step
        if fit_pitch:
            lo_p, hi_p = bp - step, bp + step
        step /= 5
    _, _, sd0 = score(0.0, 0.0)
    return {"boresight_roll_deg": round(best[1], 3), "boresight_pitch_deg": round(best[2], 3),
            "pitch_fitted": fit_pitch, "method": "vs GNSS altitude" if have_ref else "constant height",
            "spread_before_m": round(sd0, 3), "spread_after_m": round(best[4], 3),
            "height_m": round(best[3], 2), "n": len(samples),
            "roll_span_deg": round(max(rolls) - min(rolls), 1) if rolls else 0.0,
            "pitch_span_deg": round(pitch_span, 1)}
