"""Driver tests. Frames are built from the vendor protocol descriptions cited in drivers.py."""
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "daemon"))
import drivers  # noqa: E402


def nmea(body: str) -> bytes:
    return f"${body}*{drivers.nmea_xor(body):02X}\r\n".encode()


def test_nmea_xor_matches_vectornav_manual_example():
    # Register-27 read example from the VN-100 user manual (UM001), checksum 4F.
    line = ("$VNRRG,27,+006.380,+000.023,-001.953,+1.0640,-0.2531,+3.0614,+00.005,+00.344,-09.758,"
            "-0.001222,-0.000450,-0.001218*4F")
    body, cs = line[1:].split("*")
    assert drivers.nmea_xor(body) == 0x4F


def test_gga_any_talker_with_time():
    d = drivers.make_driver("nmea")
    out = d.feed(nmea("GNGGA,123519.50,3834.896,N,12129.664,W,4,14,0.9,30.0,M,46.9,M,,"))
    assert len(out) == 1
    r = out[0]
    assert r["fix"] == 4 and r["sats"] == 14 and r["alt_msl"] == 30.0
    assert r["lat"] == pytest.approx(38.58160) and r["lon"] == pytest.approx(-121.49440)
    assert r["gnss_tod"] == pytest.approx(12 * 3600 + 35 * 60 + 19.5)


def test_gga_bad_checksum_dropped_and_split_across_reads():
    d = drivers.make_driver("nmea")
    good = nmea("GPGGA,000001,3834.896,N,12129.664,W,1,08,0.9,30.0,M,46.9,M,,")
    bad = good.replace(b"*", b"0*", 1)
    assert d.feed(bad) == []
    assert d.feed(good[:20]) == [] and len(d.feed(good[20:])) == 1


def test_pashr_and_empty_pashr():
    d = drivers.make_driver("nmea_ins")
    out = d.feed(nmea("PASHR,123519.00,271.50,T,-2.10,1.30,0.00,0.05,0.05,0.10,2,0"))
    assert out == [{"roll": -2.1, "pitch": 1.3, "yaw": 271.5}]
    assert d.feed(b"$PASHR,,,T,,,,,,,0,1*21\r\n") == []


def test_vectornav_ymr_and_ins():
    d = drivers.make_driver("vectornav_ins")
    ymr = nmea("VNYMR,+120.500,-002.250,+010.000,+1.0640,-0.2531,+3.0614,+00.005,+00.344,-09.758,"
               "-0.001222,-0.000450,-0.001218")
    assert d.feed(ymr) == [{"roll": 10.0, "pitch": -2.25, "yaw": 120.5}]
    ins = nmea("VNINS,342123.5,2290,0006,+045.0,+001.5,-003.0,+38.5816000,-121.4944000,+00412.300,"
               "+050.0,+010.0,-000.5,1.0,2.5,0.3")
    r = d.feed(ins)[0]
    assert r["fix"] == 1 and r["sats"] == -1 and r["alt_hae"] == pytest.approx(412.3)
    assert r["roll"] == -3.0 and r["yaw"] == 45.0
    no_gps = nmea("VNINS,342123.5,2290,0001,+045.0,+001.5,-003.0,+0.0,+0.0,+0.0,+0,+0,+0,1,2,0.3")
    assert d.feed(no_gps)[0]["fix"] == 0


def bene(dist_cm, strength=200):
    f = bytes([0x59, 0x59, dist_cm & 0xFF, dist_cm >> 8, strength & 0xFF, strength >> 8, 0, 0])
    return f + bytes([sum(f) & 0xFF])


def test_benewake_frames_resync_and_sentinels():
    d = drivers.make_driver("benewake")
    stream = b"\x00\x13\x59" + bene(12345) + bene(18000) + bene(4500) + bene(250)
    stream = stream[:15] + stream[15:]
    out = d.feed(stream[:10]) + d.feed(stream[10:])
    assert [r.get("range_m") for r in out] == [123.45, None, None, 2.5]   # 18000/4500 = no target
    corrupt = bytearray(bene(500)); corrupt[8] ^= 0xFF
    assert d.feed(bytes(corrupt)) == []


def rvc(yaw, pitch, roll, idx=7):
    body = bytes([idx]) + struct.pack("<hhhhhh", int(yaw * 100), int(pitch * 100), int(roll * 100), 0, 0, 1000) + bytes(3)
    return b"\xAA\xAA" + body + bytes([sum(body) & 0xFF])


def test_bno08x_rvc():
    d = drivers.make_driver("bno08x_rvc")
    frame = rvc(-90.5, 3.25, -12.0)
    assert len(frame) == 19
    out = d.feed(b"\x01\xAA" + frame[:7]) + d.feed(frame[7:])
    assert out == [{"roll": -12.0, "pitch": 3.25, "yaw": pytest.approx(269.5)}]


def test_lightware_binary_roundtrip_and_crc():
    assert drivers.crc16_xmodem(b"123456789") == 0x31C3        # standard XMODEM check value
    payload = struct.pack("<II", 6123, 42)                       # 612.3 m, strength
    flags = ((len(payload) + 1) << 6)
    body = bytes([0xAA]) + struct.pack("<H", flags) + bytes([44]) + payload
    pkt = body + struct.pack("<H", drivers.crc16_xmodem(body))
    d = drivers.make_driver("lightware_binary")
    assert d.feed(b"\x00" + pkt[:5]) + d.feed(pkt[5:]) == [{"range_m": 612.3}]
    bad = bytearray(pkt); bad[5] ^= 1
    assert d.feed(bytes(bad)) == []


def test_lightware_init_packets_are_wellformed():
    init = drivers.LIGHTWARE_GRF_INIT
    assert init.startswith(b"UUU")
    pkts = init[3:]
    seen = []
    while pkts:
        plen = ((pkts[1] | pkts[2] << 8) >> 6) - 1
        total = 4 + plen + 2
        pkt = pkts[:total]
        assert struct.unpack_from("<H", pkt, total - 2)[0] == drivers.crc16_xmodem(pkt[:-2])
        seen.append(pkt[3])
        pkts = pkts[total:]
    assert seen == [74, 27, 30]


def test_ascii_sentinel_and_units():
    d = drivers.make_driver("ascii_number", invalid=[130.0])
    assert d.feed(b"98.67\r\n130.00\r\n?LD 45.10\r\nd\r\n") == [{"range_m": 98.67}, {"no_target": True}, {"range_m": 45.1}]
    assert drivers.make_driver("ascii_number", unit="cm").feed(b"12345\n") == [{"range_m": 123.45}]


def test_riegl_blocks():
    d = drivers.make_driver("riegl_ld90")
    assert d.feed(b"r0712.345 a045\r\n") == [{"range_m": 712.345}]
    assert d.feed(b"m........\r\nmOVERFLOW\r\nmSELFCHCK\r\n") == [{"no_target": True}, {"no_target": True}]


def test_mavlink_attitude_and_gps():
    pytest.importorskip("pymavlink")
    from pymavlink.dialects.v20 import common as mav2
    enc = mav2.MAVLink(None, srcSystem=1, srcComponent=1)
    att = enc.attitude_encode(1000, 0.1, -0.05, 3.0, 0, 0, 0).pack(enc)
    gps = enc.gps_raw_int_encode(0, 3, 385816000, -1214944000, 450000, 100, 100, 0, 0, 14).pack(enc)
    d = drivers.make_driver("mavlink")
    out = d.feed(att + gps)
    assert out[0]["roll"] == pytest.approx(5.7296, abs=1e-3) and out[0]["yaw"] == pytest.approx(171.887, abs=1e-2)
    assert out[1]["fix"] == 1 and out[1]["sats"] == 14 and out[1]["alt_msl"] == 450.0


def test_aglaser_ilm150_documented_format():
    """AgLaser RS232 guide: 10 characters incl. CR, e.g. 00026.56m<cr>; 99999.99m<cr> = no target."""
    d = drivers.make_driver("ascii_number", invalid=[99999.99])
    out = d.feed(b"00026.56m\r00137.20m\r99999.99m\r00000.51m\r")
    assert out == [{"range_m": 26.56}, {"range_m": 137.2}, {"no_target": True}, {"range_m": 0.51}]
