"""
SSD wear monitoring.

This mgr module accepts host-side write reports from a WearAgent, stores a
compact wear history, computes remaining life days, and publishes the result
through Ceph's existing device life-expectancy state.
"""

import errno
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from mgr_module import (
    CLIRequiresDB,
    CommandResult,
    HandleCommandResult,
    MgrModule,
    MgrModuleRecoverDB,
    Option,
)

from .cli import WearCLICommand


TIME_FORMAT = "%Y-%m-%dT%H:%M:%S.%f%z"
SECONDS_PER_DAY = 86400
NVME_DATA_UNIT_BYTES = 512000
WEAR_RLD_LOW = "DEVICE_WEAR_RLD_LOW"


@dataclass
class WearEstimate:
    rld_seconds: Optional[int]
    rld_days: Optional[float]
    confidence: str
    reason: str


def _to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def extract_smart_wear(report: Dict[str, Any]) -> Tuple[Optional[float], Optional[int]]:
    smart = report.get("smart", {})
    if not isinstance(smart, dict):
        return None, None

    pct_used = _to_float(smart.get("percentage_used"))
    if pct_used is None:
        nvme = smart.get("nvme_smart_health_information_log", {})
        if isinstance(nvme, dict):
            pct_used = _to_float(nvme.get("percentage_used"))

    data_units_written = _to_int(smart.get("data_units_written"))
    if data_units_written is None:
        nvme = smart.get("nvme_smart_health_information_log", {})
        if isinstance(nvme, dict):
            data_units_written = _to_int(nvme.get("data_units_written"))

    return pct_used, data_units_written


def estimate_remaining_life(
    percentage_used: Optional[float],
    total_written_bytes: Optional[int],
    write_rate_bps: Optional[float],
    previous_percentage_used: Optional[float] = None,
    previous_time: Optional[int] = None,
    now: Optional[int] = None,
) -> WearEstimate:
    if percentage_used is None:
        return WearEstimate(None, None, "low", "missing percentage_used")

    if percentage_used >= 100.0:
        return WearEstimate(0, 0.0, "high", "device reports full endurance used")

    remaining_pct = max(0.0, 100.0 - percentage_used)

    if (previous_percentage_used is not None and previous_time is not None
            and now is not None and now > previous_time):
        pct_delta = percentage_used - previous_percentage_used
        days_delta = (now - previous_time) / SECONDS_PER_DAY
        if pct_delta > 0.0 and days_delta > 0.0:
            pct_per_day = pct_delta / days_delta
            rld_days = remaining_pct / pct_per_day
            return WearEstimate(
                int(rld_days * SECONDS_PER_DAY),
                rld_days,
                "high",
                "smart wear slope",
            )

    if (total_written_bytes is not None and total_written_bytes > 0
            and write_rate_bps is not None and write_rate_bps > 0
            and percentage_used > 0.0):
        endurance_bytes = total_written_bytes / (percentage_used / 100.0)
        remaining_bytes = endurance_bytes * (remaining_pct / 100.0)
        rld_seconds = int(remaining_bytes / write_rate_bps)
        return WearEstimate(
            rld_seconds,
            rld_seconds / SECONDS_PER_DAY,
            "medium",
            "write-rate fallback",
        )

    return WearEstimate(None, None, "low", "insufficient write history")


def hotspot_score(hot_buckets: Any, total_write_bytes: Optional[int]) -> Optional[float]:
    if not isinstance(hot_buckets, list) or not hot_buckets or not total_write_bytes:
        return None
    hottest = 0
    for bucket in hot_buckets:
        if not isinstance(bucket, dict):
            continue
        hottest = max(hottest, _to_int(bucket.get("bytes")) or 0)
    if hottest <= 0:
        return None
    return min(1.0, hottest / float(total_write_bytes))


class Module(MgrModule):
    CLICommand = WearCLICommand

    SCHEMA = [
        """
        CREATE TABLE WearSamples (
            time INTEGER NOT NULL,
            host TEXT NOT NULL,
            devid TEXT NOT NULL,
            dev TEXT,
            host_write_bytes INTEGER,
            host_write_ops INTEGER,
            write_rate_bps REAL,
            smart_percentage_used REAL,
            smart_data_units_written INTEGER,
            rld_seconds INTEGER,
            confidence TEXT,
            reason TEXT,
            hotspot_score REAL,
            raw_report TEXT NOT NULL,
            PRIMARY KEY (time, host, devid)
        );
        """,
        """
        CREATE TABLE WearHotspots (
            time INTEGER NOT NULL,
            devid TEXT NOT NULL,
            bucket_start INTEGER NOT NULL,
            bucket_len INTEGER,
            write_bytes INTEGER,
            write_ops INTEGER,
            PRIMARY KEY (time, devid, bucket_start)
        );
        """,
        """
        CREATE TABLE WearState (
            devid TEXT PRIMARY KEY,
            host TEXT NOT NULL,
            dev TEXT,
            last_update INTEGER NOT NULL,
            wear_level REAL,
            rld_seconds INTEGER,
            confidence TEXT,
            reason TEXT,
            write_rate_bps REAL,
            hotspot_score REAL
        ) WITHOUT ROWID;
        """,
    ]

    SCHEMA_VERSIONED = [SCHEMA]

    MODULE_OPTIONS = [
        Option(
            name="enable_monitoring",
            default=True,
            type="bool",
            desc="monitor SSD wear reports",
            runtime=True,
        ),
        Option(
            name="warn_rld_days",
            default=30,
            type="int",
            desc="raise a health warning when remaining life is below this many days",
            runtime=True,
        ),
        Option(
            name="set_life_expectancy",
            default=True,
            type="bool",
            desc="write RLD estimates into Ceph device life expectancy state",
            runtime=True,
        ),
    ]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        for opt in self.MODULE_OPTIONS:
            setattr(self, opt["name"], self.get_module_option(opt["name"]))

    def config_notify(self) -> None:
        for opt in self.MODULE_OPTIONS:
            setattr(self, opt["name"], self.get_module_option(opt["name"]))
            self.log.debug(" %s = %s", opt["name"], getattr(self, opt["name"]))

    @CLIRequiresDB
    @WearCLICommand("wear report")
    @MgrModuleRecoverDB
    def report(self, inbuf: str) -> HandleCommandResult:
        if not self.enable_monitoring:
            return HandleCommandResult(
                retval=-errno.EPERM,
                stderr="wear monitoring is disabled",
            )
        if not inbuf:
            return HandleCommandResult(retval=-errno.EINVAL, stderr="missing JSON report")
        try:
            report = json.loads(inbuf)
        except ValueError as e:
            return HandleCommandResult(
                retval=-errno.EINVAL,
                stderr=f"invalid JSON report: {e}",
            )
        try:
            state = self.put_report(report)
        except ValueError as e:
            return HandleCommandResult(retval=-errno.EINVAL, stderr=str(e))
        return HandleCommandResult(stdout=json.dumps(state, sort_keys=True))

    @CLIRequiresDB
    @WearCLICommand.Read("wear status")
    @MgrModuleRecoverDB
    def status(self, devid: Optional[str] = None) -> HandleCommandResult:
        rows = self.get_state(devid)
        return HandleCommandResult(stdout=json.dumps(rows, indent=4, sort_keys=True))

    def put_report(self, report: Dict[str, Any]) -> Dict[str, Any]:
        normalized = self.normalize_report(report)
        prev = self.get_previous_sample(normalized["devid"], normalized["time"])
        estimate = estimate_remaining_life(
            normalized["smart_percentage_used"],
            normalized["total_written_bytes"],
            normalized["write_rate_bps"],
            previous_percentage_used=prev.get("smart_percentage_used") if prev else None,
            previous_time=prev.get("time") if prev else None,
            now=normalized["time"],
        )
        normalized["rld_seconds"] = estimate.rld_seconds
        normalized["confidence"] = estimate.confidence
        normalized["reason"] = estimate.reason
        normalized["hotspot_score"] = hotspot_score(
            report.get("hot_buckets"),
            normalized["host_write_bytes"],
        )

        with self._db_lock, self.db:
            self.db.execute("BEGIN;")
            self._insert_sample(normalized, report)
            self._insert_hotspots(normalized, report.get("hot_buckets"))
            self._upsert_state(normalized)

        if normalized["smart_percentage_used"] is not None:
            self.set_device_wear_level(
                normalized["devid"],
                normalized["smart_percentage_used"] / 100.0,
            )
        if self.set_life_expectancy and estimate.rld_seconds is not None:
            self.set_device_life_expectancy(normalized["devid"], estimate.rld_seconds)
        self.update_health()
        return self.state_row_to_dict(normalized)

    def normalize_report(self, report: Dict[str, Any]) -> Dict[str, Any]:
        host = report.get("host")
        devid = report.get("devid")
        if not isinstance(host, str) or not host:
            raise ValueError("report.host must be a non-empty string")
        if not isinstance(devid, str) or not devid:
            raise ValueError("report.devid must be a non-empty string")

        now = _to_int(report.get("ts"))
        if now is None:
            now = int(datetime.now(timezone.utc).timestamp())

        pct_used, data_units_written = extract_smart_wear(report)
        total_written_bytes = None
        if data_units_written is not None:
            total_written_bytes = data_units_written * NVME_DATA_UNIT_BYTES

        return {
            "time": now,
            "host": host,
            "devid": devid,
            "dev": report.get("dev") if isinstance(report.get("dev"), str) else None,
            "host_write_bytes": _to_int(report.get("host_write_bytes_delta")),
            "host_write_ops": _to_int(report.get("host_write_ops_delta")),
            "write_rate_bps": _to_float(report.get("write_rate_bps")),
            "smart_percentage_used": pct_used,
            "smart_data_units_written": data_units_written,
            "total_written_bytes": total_written_bytes,
            "rld_seconds": None,
            "confidence": "low",
            "reason": "",
            "hotspot_score": None,
        }

    def get_previous_sample(self, devid: str, before: int) -> Optional[Dict[str, Any]]:
        sql = """
            SELECT time, smart_percentage_used
              FROM WearSamples
             WHERE devid = ? AND time < ? AND smart_percentage_used IS NOT NULL
             ORDER BY time DESC
             LIMIT 1;
        """
        with self._db_lock, self.db:
            cur = self.db.execute(sql, (devid, before))
            row = cur.fetchone()
        if not row:
            return None
        return {"time": row["time"], "smart_percentage_used": row["smart_percentage_used"]}

    def _insert_sample(self, normalized: Dict[str, Any], report: Dict[str, Any]) -> None:
        sql = """
            INSERT OR REPLACE INTO WearSamples (
                time, host, devid, dev, host_write_bytes, host_write_ops,
                write_rate_bps, smart_percentage_used, smart_data_units_written,
                rld_seconds, confidence, reason, hotspot_score, raw_report
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """
        self.db.execute(sql, (
            normalized["time"],
            normalized["host"],
            normalized["devid"],
            normalized["dev"],
            normalized["host_write_bytes"],
            normalized["host_write_ops"],
            normalized["write_rate_bps"],
            normalized["smart_percentage_used"],
            normalized["smart_data_units_written"],
            normalized["rld_seconds"],
            normalized["confidence"],
            normalized["reason"],
            normalized["hotspot_score"],
            json.dumps(report, sort_keys=True),
        ))

    def _insert_hotspots(self, normalized: Dict[str, Any], hot_buckets: Any) -> None:
        if not isinstance(hot_buckets, list):
            return
        sql = """
            INSERT OR REPLACE INTO WearHotspots (
                time, devid, bucket_start, bucket_len, write_bytes, write_ops
            ) VALUES (?, ?, ?, ?, ?, ?);
        """
        for bucket in hot_buckets:
            if not isinstance(bucket, dict):
                continue
            bucket_start = _to_int(bucket.get("offset"))
            if bucket_start is None:
                continue
            self.db.execute(sql, (
                normalized["time"],
                normalized["devid"],
                bucket_start,
                _to_int(bucket.get("len")),
                _to_int(bucket.get("bytes")),
                _to_int(bucket.get("ops")),
            ))

    def _upsert_state(self, normalized: Dict[str, Any]) -> None:
        sql = """
            INSERT OR REPLACE INTO WearState (
                devid, host, dev, last_update, wear_level, rld_seconds,
                confidence, reason, write_rate_bps, hotspot_score
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """
        wear_level = None
        if normalized["smart_percentage_used"] is not None:
            wear_level = normalized["smart_percentage_used"] / 100.0
        self.db.execute(sql, (
            normalized["devid"],
            normalized["host"],
            normalized["dev"],
            normalized["time"],
            wear_level,
            normalized["rld_seconds"],
            normalized["confidence"],
            normalized["reason"],
            normalized["write_rate_bps"],
            normalized["hotspot_score"],
        ))

    def get_state(self, devid: Optional[str] = None) -> List[Dict[str, Any]]:
        if devid:
            sql = "SELECT * FROM WearState WHERE devid = ? ORDER BY devid;"
            args = (devid,)
        else:
            sql = "SELECT * FROM WearState ORDER BY devid;"
            args = ()
        with self._db_lock, self.db:
            cur = self.db.execute(sql, args)
            rows = cur.fetchall()
        return [self.state_row_to_dict(dict(row)) for row in rows]

    def state_row_to_dict(self, row: Dict[str, Any]) -> Dict[str, Any]:
        rld_seconds = row.get("rld_seconds")
        wear_level = row.get("wear_level")
        if wear_level is None and row.get("smart_percentage_used") is not None:
            wear_level = row.get("smart_percentage_used") / 100.0
        return {
            "devid": row.get("devid"),
            "host": row.get("host"),
            "dev": row.get("dev"),
            "last_update": row.get("last_update", row.get("time")),
            "wear_level": wear_level,
            "rld_seconds": rld_seconds,
            "rld_days": None if rld_seconds is None else rld_seconds / SECONDS_PER_DAY,
            "confidence": row.get("confidence"),
            "reason": row.get("reason"),
            "write_rate_bps": row.get("write_rate_bps"),
            "hotspot_score": row.get("hotspot_score"),
        }

    def set_device_life_expectancy(self, devid: str, rld_seconds: int) -> None:
        when = datetime.fromtimestamp(
            int(datetime.now(timezone.utc).timestamp()) + rld_seconds,
            timezone.utc,
        ).strftime(TIME_FORMAT)
        result = CommandResult("")
        self.send_command(result, "mon", "", json.dumps({
            "prefix": "device set-life-expectancy",
            "format": "json",
            "devid": devid,
            "from": when,
            "to": when,
        }), "")
        r, outb, outs = result.wait()
        if r != 0:
            self.log.warning(
                "failed to set life expectancy for %s: r=%s out=%s err=%s",
                devid,
                r,
                outb,
                outs,
            )

    def update_health(self) -> None:
        warn_seconds = int(self.warn_rld_days) * SECONDS_PER_DAY
        warnings = []
        for row in self.get_state():
            rld_seconds = row.get("rld_seconds")
            if rld_seconds is not None and rld_seconds <= warn_seconds:
                warnings.append(
                    "%s on %s has %.1f days remaining (%s confidence, %s)" % (
                        row.get("devid"),
                        row.get("host"),
                        rld_seconds / SECONDS_PER_DAY,
                        row.get("confidence"),
                        row.get("reason"),
                    )
                )
        if warnings:
            self.set_health_checks({
                WEAR_RLD_LOW: {
                    "severity": "warning",
                    "summary": "%d device(s) below wear remaining-life threshold" % len(warnings),
                    "count": len(warnings),
                    "detail": warnings,
                }
            })
        else:
            self.set_health_checks({})
