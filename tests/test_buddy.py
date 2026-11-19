import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "daemon"))
import buddy  # noqa: E402
import main  # noqa: E402


def make(**kw):
    s = main.TelemetryState(gnss_ok=True, gps_fix=1, sats=11, laser_ok=True, laser_alive=True, imu_ok=True,
                            logging_active=True, agl_ft=500, agl_m=152, low_agl_warn_ft=200, log_file="f.csv")
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def b(cfg=None):
    bd = buddy.Buddy(cfg or {"payload_sync": {"enabled": False}})
    bd.started -= 60          # past the warm-up window
    bd._slow = {}             # no disk/temp/power facts in unit tests
    return bd


def texts(bd):
    return [e.text for e in bd.events]


def test_sensor_lost_and_back_are_edge_triggered(tmp_path):
    bd = b()
    bd.evaluate(make(), tmp_path, False)
    assert texts(bd) == []
    bd.evaluate(make(laser_ok=False, laser_alive=False), tmp_path, False)
    bd.evaluate(make(laser_ok=False, laser_alive=False), tmp_path, False)   # still lost: no repeat
    bd.evaluate(make(), tmp_path, False)
    assert texts(bd) == ["Laser altimeter lost", "Laser altimeter back"]
    assert [e.level for e in bd.events] == ["alert", "info"]


def test_never_seen_sensor_warns_once_after_warmup(tmp_path):
    bd = b()
    for _ in range(3):
        bd.evaluate(make(imu_ok=False), tmp_path, False)
    assert texts(bd) == ["Attitude sensor: no data yet. Check port and baud."]


def test_unconfigured_sensor_is_silent(tmp_path):
    bd = b()
    bd.configured = {"gnss": True, "laser": True, "imu": False}
    bd.evaluate(make(imu_ok=False), tmp_path, False)
    assert texts(bd) == []


def test_low_agl_has_hysteresis(tmp_path):
    bd = b()
    for ft in (500, 190, 205, 199, 230):
        bd.evaluate(make(agl_ft=ft), tmp_path, False)
    # 205 is inside the 20 ft band, so it does not clear; 230 does.
    assert texts(bd) == ["Below 200 feet AGL", "Back above 200 feet AGL"]


def test_gnss_fix_and_satellites(tmp_path):
    bd = b()
    bd.evaluate(make(gps_fix=0, sats=3), tmp_path, False)
    bd.evaluate(make(gps_fix=1, sats=4), tmp_path, False)
    bd.evaluate(make(gps_fix=1, sats=9), tmp_path, False)
    assert texts(bd) == ["GNSS has no position fix", "GNSS fix acquired", "Only 4 satellites",
                         "Satellite count recovered"]


def test_sd_card_fallback_warns_once(tmp_path):
    bd = b()
    for _ in range(3):
        bd.evaluate(make(), tmp_path, True)
    assert texts(bd) == ["Logging to the SD card: SSD not mounted"]


def test_trigger_stall(tmp_path):
    bd = b({"payload_sync": {"enabled": True, "trigger_hz": 1.0}})
    t0 = 1_000_000.0
    bd.evaluate(make(trigger_count=1), tmp_path, False, now=t0)
    bd.evaluate(make(trigger_count=1), tmp_path, False, now=t0 + 5)
    assert texts(bd) == []
    bd.evaluate(make(trigger_count=1), tmp_path, False, now=t0 + 9)
    bd.evaluate(make(trigger_count=2), tmp_path, False, now=t0 + 10)
    assert texts(bd) == ["Payload trigger stalled", "Payload trigger running"]


def test_ground_check_mounting_offset(tmp_path):
    bd = b()
    items = {i["item"]: i for i in bd.ground_check(make(agl_m=0.1), {}, tmp_path, False)}
    assert items["Laser altimeter"]["status"] == "ok"
    items = {i["item"]: i for i in bd.ground_check(make(agl_m=1.2), {}, tmp_path, False)}
    assert items["Laser altimeter"]["status"] == "warn"
    assert "mounting_offset_m" in items["Laser altimeter"]["detail"]


def test_ground_check_flags_missing_and_sim(tmp_path):
    bd = b()
    items = {i["item"]: i["status"] for i in bd.ground_check(make(gnss_ok=False, sim_mode=True), {}, tmp_path, False)}
    assert items["GNSS"] == "fail" and items["Mode"] == "warn"


def ctx():
    s = make(trigger_count=42)
    return b().context(s, {}, {"rows": 100, "duration_min": 3.0, "agl_ft_min": 150, "agl_ft_max": 600, "callouts": 2})


@pytest.mark.parametrize("q,expect", [
    ("How many triggers so far?", "42 payload triggers"),
    ("What's my AGL?", "500 ft AGL"),
    ("Should I descend over the ice?", "pilot's call"),
    ("Is it safe to fly into that cloud?", "pilot's call"),
    ("Quick status", "3 min logged"),
])
def test_rules_answer_common_questions(q, expect):
    assert expect in buddy.rule_answer(q, ctx(), strict=True)


def test_open_question_goes_to_model():
    assert buddy.rule_answer("Anything unusual in the data?", ctx(), strict=True) is None


def test_why_dropout_reports_facts_not_a_guess():
    c = ctx()
    c["recent_callouts"] = [{"id": 1, "ts": 1000.0, "level": "alert", "text": "Laser altimeter lost"},
                            {"id": 2, "ts": 1004.0, "level": "info", "text": "Laser altimeter back"}]
    a = buddy.rule_answer("Why did the laser drop out?", c, strict=True)
    assert "lost at 00:16:40Z for 4 s" in a and "shows when, not why" in a


def test_ask_falls_back_without_ollama():
    bd = buddy.Buddy({"buddy": {"ai": True, "ollama_url": "http://127.0.0.1:9", "model": "x"}})

    async def run():
        return "".join([c async for c in bd.ask_stream("Anything unusual in the data?", ctx())])

    out = asyncio.run(run())
    assert "did not answer" in out and "GNSS: OK" in out


def test_context_text_is_compact():
    t = buddy.context_text(ctx())
    assert "AGL 500 ft" in t and len(t) < 1200


def test_laser_no_return_is_not_laser_lost(tmp_path):
    bd = b()
    bd.evaluate(make(), tmp_path, False)
    bd.evaluate(make(laser_ok=False, laser_alive=True), tmp_path, False)   # talking, "99999.99m"
    bd.evaluate(make(), tmp_path, False)
    assert texts(bd) == ["Laser altimeter: no ground return", "Laser altimeter: ground return back"]
    items = {i["item"]: i for i in bd.ground_check(make(laser_ok=False, laser_alive=True), {}, tmp_path, False)}
    assert items["Laser altimeter"]["status"] == "warn" and "no ground return" in items["Laser altimeter"]["detail"]
