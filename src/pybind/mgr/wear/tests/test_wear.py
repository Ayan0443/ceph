import sqlite3
import threading

import pytest

import wear.module as wear_mod


def make_mgr():
    mgr = wear_mod.Module.__new__(wear_mod.Module)
    mgr._db_lock = threading.Lock()
    mgr._db = sqlite3.connect(":memory:", isolation_level=None)
    mgr._db.row_factory = sqlite3.Row
    for sql in wear_mod.Module.SCHEMA:
        mgr._db.execute(sql)
    mgr.warn_rld_days = 100
    mgr.set_life_expectancy = True
    mgr.enable_monitoring = True
    mgr.wear_calls = []
    mgr.life_calls = []
    mgr.health_checks = []
    mgr.set_device_wear_level = lambda devid, wear: mgr.wear_calls.append((devid, wear))
    mgr.set_device_life_expectancy = (
        lambda devid, rld_seconds: mgr.life_calls.append((devid, rld_seconds))
    )
    mgr.set_health_checks = lambda checks: mgr.health_checks.append(checks)
    return mgr


def test_extract_smart_wear_accepts_flat_and_nested_nvme_fields():
    assert wear_mod.extract_smart_wear({
        "smart": {"percentage_used": "12", "data_units_written": "34"}
    }) == (12.0, 34)

    assert wear_mod.extract_smart_wear({
        "smart": {
            "nvme_smart_health_information_log": {
                "percentage_used": 7,
                "data_units_written": 99,
            }
        }
    }) == (7.0, 99)


def test_estimate_remaining_life_prefers_smart_wear_slope():
    estimate = wear_mod.estimate_remaining_life(
        percentage_used=12.0,
        total_written_bytes=10_000_000,
        write_rate_bps=1,
        previous_percentage_used=10.0,
        previous_time=1_000_000,
        now=1_000_000 + (2 * wear_mod.SECONDS_PER_DAY),
    )

    assert estimate.confidence == "high"
    assert estimate.reason == "smart wear slope"
    assert estimate.rld_days == pytest.approx(88.0)
    assert estimate.rld_seconds == 88 * wear_mod.SECONDS_PER_DAY


def test_estimate_remaining_life_falls_back_to_write_rate():
    estimate = wear_mod.estimate_remaining_life(
        percentage_used=25.0,
        total_written_bytes=1000,
        write_rate_bps=1,
    )

    assert estimate.confidence == "medium"
    assert estimate.reason == "write-rate fallback"
    assert estimate.rld_seconds == 3000


def test_put_report_persists_samples_state_hotspots_and_updates_device_state():
    mgr = make_mgr()
    base_report = {
        "host": "host-a",
        "devid": "nvme-SERIAL",
        "dev": "nvme0n1",
        "ts": 1_000_000,
        "host_write_bytes_delta": 1000,
        "host_write_ops_delta": 10,
        "write_rate_bps": 1,
        "smart": {
            "percentage_used": 10,
            "data_units_written": 100,
        },
        "hot_buckets": [
            {"offset": 0, "len": 1024, "bytes": 500, "ops": 5},
        ],
    }
    mgr.put_report(base_report)

    next_report = dict(base_report)
    next_report["ts"] = base_report["ts"] + wear_mod.SECONDS_PER_DAY
    next_report["host_write_bytes_delta"] = 2000
    next_report["smart"] = {
        "percentage_used": 11,
        "data_units_written": 120,
    }
    next_report["hot_buckets"] = [
        {"offset": 0, "len": 1024, "bytes": 1200, "ops": 12},
        {"offset": 1024, "len": 1024, "bytes": 800, "ops": 8},
    ]
    state = mgr.put_report(next_report)

    assert state["devid"] == "nvme-SERIAL"
    assert state["wear_level"] == pytest.approx(0.11)
    assert state["confidence"] == "high"
    assert state["reason"] == "smart wear slope"
    assert state["rld_days"] == pytest.approx(89.0)
    assert state["hotspot_score"] == pytest.approx(0.6)

    with mgr._db_lock, mgr.db:
        sample_count = mgr.db.execute("SELECT COUNT(*) FROM WearSamples").fetchone()[0]
        hotspot_count = mgr.db.execute("SELECT COUNT(*) FROM WearHotspots").fetchone()[0]
        stored_state = mgr.db.execute(
            "SELECT * FROM WearState WHERE devid = ?", ("nvme-SERIAL",)
        ).fetchone()

    assert sample_count == 2
    assert hotspot_count == 3
    assert stored_state["rld_seconds"] == 89 * wear_mod.SECONDS_PER_DAY
    assert mgr.wear_calls[-1] == ("nvme-SERIAL", pytest.approx(0.11))
    assert mgr.life_calls[-1] == ("nvme-SERIAL", 89 * wear_mod.SECONDS_PER_DAY)
    assert wear_mod.WEAR_RLD_LOW in mgr.health_checks[-1]


def test_normalize_report_rejects_missing_identifiers():
    mgr = make_mgr()

    with pytest.raises(ValueError, match="report.host"):
        mgr.normalize_report({"devid": "dev1"})

    with pytest.raises(ValueError, match="report.devid"):
        mgr.normalize_report({"host": "host-a"})
