"""
SSD 磨损监控模块。

该 MGR 模块接收 WearAgent 的主机侧写入报告，保存精简磨损历史，计算剩余
寿命天数，并通过 Ceph 现有的设备预期寿命状态发布结果。
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


# Ceph 设备预期寿命接口使用的 UTC 时间格式。
TIME_FORMAT = "%Y-%m-%dT%H:%M:%S.%f%z"
# 一天包含的秒数，用于 RLD 秒/天转换。
SECONDS_PER_DAY = 86400
# NVMe SMART data_units_written 的单个数据单元大小，单位：字节。
NVME_DATA_UNIT_BYTES = 512000
# 剩余寿命过低时发布的 Ceph 健康检查标识。
WEAR_RLD_LOW = "DEVICE_WEAR_RLD_LOW"


@dataclass
class WearEstimate:
    """表示一次剩余寿命估算结果及其证据可信度。"""

    rld_seconds: Optional[int]  # 预计剩余寿命，单位：秒；无法估算时为 None。
    rld_days: Optional[float]  # 预计剩余寿命，单位：天；无法估算时为 None。
    confidence: str  # 可信度等级：high、medium 或 low。
    reason: str  # 采用的估算方法或无法估算的原因。


def _to_float(value: Any) -> Optional[float]:
    """将可选上报值转换为浮点数，转换失败时不拒绝整份报告。"""

    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> Optional[int]:
    """将可选上报值转换为整数，转换失败时不拒绝整份报告。"""

    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def extract_smart_wear(report: Dict[str, Any]) -> Tuple[Optional[float], Optional[int]]:
    """从扁平字段或 smartctl 风格的 NVMe JSON 中提取耐久度计数器。"""
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
    """估算剩余寿命，优先采用 SMART 磨损斜率，历史不足时使用写入速率。"""
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
    """返回本采样周期内最热逻辑区域的写入占比。"""

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
    """汇聚主机磨损报告，并提供 SSD 当前剩余寿命状态。"""

    CLICommand = WearCLICommand

    SCHEMA = [
        # WearSamples 保存每次设备上报：
        # time=Unix 秒；host=主机名；devid=Ceph 稳定设备 ID；dev=内核设备名；
        # host_write_bytes/ops=周期写入字节数/操作数；write_rate_bps=字节/秒；
        # smart_percentage_used=SMART 已用耐久度百分比；
        # smart_data_units_written=SMART 累计 NVMe 数据单元数；rld_seconds=剩余秒数；
        # confidence/reason=估算可信度/依据；hotspot_score=最热区域写入占比；
        # raw_report=Agent 原始 JSON。
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
        # WearHotspots 保存每个采样周期的热点：time=Unix 秒；devid=设备 ID；
        # bucket_start/bucket_len=逻辑区域起点/长度（字节）；
        # write_bytes/write_ops=本周期落入该区域的写入字节数/操作数。
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
        # WearState 只保存每台设备的最新状态：devid/host/dev=设备身份；
        # last_update=最后上报 Unix 秒；wear_level=0.0~1.0 磨损比例；
        # rld_seconds=剩余寿命秒数；confidence/reason=可信度/估算依据；
        # write_rate_bps=字节/秒；hotspot_score=最热区域写入占比。
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
        """初始化模块并缓存可在运行时调整的配置项。"""

        super().__init__(*args, **kwargs)
        for opt in self.MODULE_OPTIONS:
            setattr(self, opt["name"], self.get_module_option(opt["name"]))

    def config_notify(self) -> None:
        """Ceph 配置发生变化后刷新缓存的配置项。"""

        for opt in self.MODULE_OPTIONS:
            setattr(self, opt["name"], self.get_module_option(opt["name"]))
            self.log.debug(" %s = %s", opt["name"], getattr(self, opt["name"]))

    # 原因：必须注册经过数据库就绪检查的包装函数；装饰器顺序反转会保存
    # 未受保护的函数，并向 CLI 暴露 MgrDBNotReady 调用栈。
    @WearCLICommand("wear report")
    @CLIRequiresDB
    @MgrModuleRecoverDB
    def report(self, inbuf: str) -> HandleCommandResult:
        """校验一份 Agent JSON 报告，持久化数据并生成设备状态。"""

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

    # 原因：就绪检查必须位于已注册命令内部，与 wear report 保持一致；
    # 当 .mgr pool 不可用时返回 EAGAIN。
    @WearCLICommand.Read("wear status")
    @CLIRequiresDB
    @MgrModuleRecoverDB
    def status(self, devid: Optional[str] = None) -> HandleCommandResult:
        """返回指定设备或全部已知设备的当前磨损状态。"""

        rows = self.get_state(devid)
        return HandleCommandResult(stdout=json.dumps(rows, indent=4, sort_keys=True))

    def put_report(self, report: Dict[str, Any]) -> Dict[str, Any]:
        """规范化、估算、持久化并发布一份磨损报告。"""

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
        """将不可信的 Agent 字段转换为数据库使用的强类型结构。"""

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
            "time": now,  # 采样时间，Unix 秒。
            "host": host,  # 采集节点主机名。
            "devid": devid,  # Ceph 稳定设备 ID。
            "dev": report.get("dev") if isinstance(report.get("dev"), str) else None,
            # 周期主机写入增量，分别为字节和操作次数。
            "host_write_bytes": _to_int(report.get("host_write_bytes_delta")),
            "host_write_ops": _to_int(report.get("host_write_ops_delta")),
            "write_rate_bps": _to_float(report.get("write_rate_bps")),  # 字节/秒。
            "smart_percentage_used": pct_used,  # SMART 已用耐久度百分比。
            "smart_data_units_written": data_units_written,  # 累计 NVMe 数据单元。
            "total_written_bytes": total_written_bytes,  # SMART 推导的累计写入字节。
            "rld_seconds": None,  # 后续计算填充的剩余寿命秒数。
            "confidence": "low",  # 后续计算填充的可信度等级。
            "reason": "",  # 后续计算填充的估算依据。
            "hotspot_score": None,  # 后续计算填充的最热区域写入占比。
        }

    def get_previous_sample(self, devid: str, before: int) -> Optional[Dict[str, Any]]:
        """读取更早的最新 SMART 磨损点，用于计算变化斜率。"""

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
        """同时保存规范化指标和原始上报内容。"""

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
        """保存合法热点区域，并忽略格式错误的条目。"""

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
        """写入或替换设备的精简最新状态记录。"""

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
        """读取精简状态，可按设备 ID 过滤。"""

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
        """将数据库记录转换为稳定的 CLI JSON 字段。"""

        rld_seconds = row.get("rld_seconds")
        wear_level = row.get("wear_level")
        if wear_level is None and row.get("smart_percentage_used") is not None:
            wear_level = row.get("smart_percentage_used") / 100.0
        return {
            "devid": row.get("devid"),  # Ceph 稳定设备 ID。
            "host": row.get("host"),  # 最近一次上报该设备的主机名。
            "dev": row.get("dev"),  # 最近一次上报的内核设备名。
            "last_update": row.get("last_update", row.get("time")),  # Unix 秒。
            "wear_level": wear_level,  # 0.0~1.0 的已用耐久度比例。
            "rld_seconds": rld_seconds,  # 预计剩余寿命，单位：秒。
            "rld_days": None if rld_seconds is None else rld_seconds / SECONDS_PER_DAY,
            "confidence": row.get("confidence"),  # high、medium 或 low。
            "reason": row.get("reason"),  # 寿命估算方法或失败原因。
            "write_rate_bps": row.get("write_rate_bps"),  # 字节/秒。
            "hotspot_score": row.get("hotspot_score"),  # 0.0~1.0 热点写入占比。
        }

    def set_device_life_expectancy(self, devid: str, rld_seconds: int) -> None:
        """通过 Ceph 现有设备寿命接口发布剩余寿命。"""
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
        """设置或清除剩余寿命过低的集群健康告警。"""

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
