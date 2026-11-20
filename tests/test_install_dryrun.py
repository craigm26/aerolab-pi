"""Tests for install.sh --dry-run mode and syntax check."""

import os
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
LOCAL_CONFIG = REPO_ROOT / "config" / "sensors.local.yaml"


@pytest.fixture
def preserve_local_config():
    """Keep config/sensors.local.yaml untouched across each test."""
    existed = LOCAL_CONFIG.exists()
    saved = LOCAL_CONFIG.read_bytes() if existed else None
    if existed:
        LOCAL_CONFIG.unlink()
    try:
        yield
    finally:
        if existed and saved is not None:
            LOCAL_CONFIG.write_bytes(saved)
        elif LOCAL_CONFIG.exists():
            LOCAL_CONFIG.unlink()


def _run_install(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "install.sh", *args],
        cwd=REPO_ROOT,
        env=dict(os.environ),
        capture_output=True,
        text=True,
        timeout=60,
    )


def _assert_core_dryrun_output(output: str):
    lines = output.splitlines()
    assert any("[dry-run]" in line and "apt-get" in line for line in lines)
    assert any("[dry-run]" in line and "python3 -m venv" in line for line in lines)
    assert any("[dry-run]" in line and "pip" in line for line in lines)
    assert any("[dry-run]" in line and "usermod" in line for line in lines)
    assert any("[dry-run]" in line and "systemctl stop" in line for line in lines)
    assert any("[dry-run]" in line and "systemctl daemon-reload" in line for line in lines)
    assert any("[dry-run]" in line and "systemctl enable" in line for line in lines)
    assert any("[dry-run]" in line and "systemctl restart" in line for line in lines)
    assert "NOPASSWD: /usr/bin/systemctl poweroff" in output
    assert 'ATTR{latency_timer}="1"' in output
    assert "WatchdogSec=60" in output


def test_install_bash_syntax():
    res = subprocess.run(
        ["bash", "-n", "install.sh"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert res.returncode == 0, res.stderr


@pytest.mark.parametrize(
    "flags",
    [
        ("--dry-run", "--defaults"),
        ("--dry-run", "--sim"),
        ("--defaults", "--dry-run"),
        ("--sim", "--dry-run"),
    ],
)
def test_install_dryrun_defaults_and_sim(preserve_local_config, flags):
    res = _run_install(*flags)
    assert res.returncode == 0, res.stderr
    _assert_core_dryrun_output(res.stdout)
    assert not LOCAL_CONFIG.exists(), "dry-run must not create config/sensors.local.yaml"


def test_install_unknown_flag_exits_2():
    res = _run_install("--dry-run", "--not-a-real-flag")
    assert res.returncode == 2
    assert "Unknown option" in (res.stdout + res.stderr)


def test_install_dryrun_prints_optional_extras(preserve_local_config):
    """When sensors.local.yaml asks for MAVLink, PPS and Ollama, dry-run prints all three."""
    cfg = {
        "mode": "live",
        "server": {"port": 8080},
        "gnss": {"driver": "mavlink"},
        "timing": {"pps_device": "pps0", "pps_pin": 25},
        "buddy": {"ai": True, "model": "qwen3.5:2b"},
    }
    LOCAL_CONFIG.write_text(yaml.safe_dump(cfg, sort_keys=False))
    before = LOCAL_CONFIG.read_bytes()

    res = _run_install("--dry-run", "--defaults")
    assert res.returncode == 0, res.stderr
    assert any("[dry-run]" in line and "pymavlink" in line for line in res.stdout.splitlines())
    assert "dtoverlay=pps-" in res.stdout
    assert "pps-tools" in res.stdout
    assert any("[dry-run]" in line and "ollama pull qwen3.5:2b" in line for line in res.stdout.splitlines())
    assert "chrony" not in res.stdout
    assert LOCAL_CONFIG.read_bytes() == before


def test_install_dryrun_prints_chrony_without_pps(preserve_local_config):
    """When timing.set_clock is true without PPS, dry-run configures chrony SOCK without noselect or PPS."""
    cfg = {
        "mode": "live",
        "server": {"port": 8000},
        "gnss": {"driver": "nmea"},
        "timing": {"pps_device": "", "set_clock": True},
    }
    LOCAL_CONFIG.write_text(yaml.safe_dump(cfg, sort_keys=False))
    before = LOCAL_CONFIG.read_bytes()

    res = _run_install("--dry-run", "--defaults")
    assert res.returncode == 0, res.stderr
    assert any("[dry-run]" in line and "apt-get install -y chrony" in line for line in res.stdout.splitlines())
    assert any("[dry-run]" in line and "usermod -aG _chrony" in line for line in res.stdout.splitlines())
    assert "/etc/chrony/conf.d/aerolab.conf" in res.stdout
    assert "refclock SOCK /run/chrony/aerolab.sock refid GNSS" in res.stdout
    assert "noselect" not in res.stdout
    assert "refclock PPS" not in res.stdout
    assert "makestep 1.0 -1" in res.stdout
    assert "/etc/systemd/system/chrony.service.d/aerolab.conf" in res.stdout
    assert "ExecStartPost=" in res.stdout
    assert any("[dry-run]" in line and "systemctl restart chrony" in line for line in res.stdout.splitlines())
    assert LOCAL_CONFIG.read_bytes() == before


def test_install_dryrun_prints_chrony_with_pps(preserve_local_config):
    """When both timing.set_clock and PPS are configured, dry-run locks PPS to GNSS and marks SOCK noselect."""
    cfg = {
        "mode": "live",
        "server": {"port": 8000},
        "gnss": {"driver": "nmea"},
        "timing": {"pps_device": "pps0", "pps_pin": 18, "set_clock": True},
    }
    LOCAL_CONFIG.write_text(yaml.safe_dump(cfg, sort_keys=False))
    before = LOCAL_CONFIG.read_bytes()

    res = _run_install("--dry-run", "--defaults")
    assert res.returncode == 0, res.stderr
    assert "refclock SOCK /run/chrony/aerolab.sock refid GNSS offset 0.05 delay 0.2 noselect" in res.stdout
    assert "refclock PPS /dev/pps0 lock GNSS refid PPS" in res.stdout
    assert "makestep 1.0 -1" in res.stdout
    assert LOCAL_CONFIG.read_bytes() == before
