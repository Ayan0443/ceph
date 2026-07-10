"""
主机侧 SSD 磨损采集辅助模块。

WearAgent 可将 eBPF 写事件送入 HotspotTracker；当 BPF 不可用或被主动禁用时，
本模块也提供 /proc/diskstats 降级采集路径。
"""

import argparse
import json
import logging
import socket
import subprocess
from dataclasses import dataclass
from time import sleep, time
from typing import Any, Callable, Dict, Iterable, List, Optional


# 内核 diskstats 和块层 tracepoint 使用的默认扇区大小，单位：字节。
DEFAULT_SECTOR_SIZE = 512
# 逻辑热点桶默认大小，单位：字节；当前默认值为 1 GiB。
DEFAULT_BUCKET_SIZE = 1 << 30

LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class DiskStat:
    """保存单个块设备的内核累计写入计数快照。"""

    major: int  # 主设备号，来源：/proc/diskstats 第 1 列。
    minor: int  # 次设备号，来源：/proc/diskstats 第 2 列。
    name: str  # 内核块设备名，例如 nvme0n1。
    writes_completed: int  # 启动以来完成的累计写操作数。
    sectors_written: int  # 启动以来写入的累计扇区数。


@dataclass(frozen=True)
class WriteDelta:
    """表示两次 diskstats 快照之间观测到的写入增量。"""

    write_bytes: int  # 本采样周期写入字节增量，单位：字节。
    write_ops: int  # 本采样周期完成的写操作增量，单位：次。
    write_rate_bps: float  # 周期平均写入速率，单位：字节/秒。


@dataclass(frozen=True)
class DeviceRef:
    """关联内核设备名与 Ceph 稳定设备 ID。"""

    dev: str  # 内核设备名，例如 nvme0n1。
    devid: str  # Ceph inventory 中的稳定设备 ID，例如 NVMe 序列标识。


class SmartctlError(RuntimeError):
    """表示 smartctl 执行失败或 JSON 解析失败。"""


def dev_path(dev: str) -> str:
    """将内核设备名规范化为绝对设备路径。"""

    return dev if dev.startswith("/dev/") else f"/dev/{dev}"


def collect_smart(dev: str, timeout: int = 30, runner: Optional[Callable[..., Any]] = None) -> Dict[str, Any]:
    """以只读方式采集完整 SMART 数据，不执行会修改设备状态的命令。"""

    if not dev:
        raise ValueError("dev must be non-empty")
    run = subprocess.run if runner is None else runner
    proc = run(
        ["smartctl", "-x", "--json", dev_path(dev)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        raise SmartctlError(proc.stderr or proc.stdout or "smartctl failed")
    try:
        data = json.loads(proc.stdout)
    except ValueError as e:
        raise SmartctlError("smartctl returned invalid JSON") from e
    if not isinstance(data, dict):
        raise SmartctlError("smartctl JSON root is not an object")
    return data


def diskstat_key(stat: DiskStat) -> str:
    """返回与 eBPF 采集器一致的 major:minor 设备键。"""

    return f"{stat.major}:{stat.minor}"


def parse_device_ids(device_ids: str) -> Dict[str, str]:
    """将 OSD 元数据中的 device_ids 字段解析为设备映射。"""

    devices = {}
    for item in device_ids.replace(";", ",").replace(" ", ",").split(","):
        if not item or "=" not in item:
            continue
        dev, devid = item.split("=", 1)
        if dev and devid:
            devices[dev] = devid
    return devices


def parse_osd_metadata_devices(metadata: Iterable[Dict[str, Any]]) -> Dict[str, str]:
    """合并全部 OSD 元数据记录中声明的设备映射。"""

    devices = {}
    for osd in metadata:
        device_ids = osd.get("device_ids")
        if isinstance(device_ids, str):
            devices.update(parse_device_ids(device_ids))
    return devices


def load_ceph_device_map(
    ceph_bin: str = "ceph",
    runner: Optional[Callable[..., Any]] = None,
) -> Dict[str, str]:
    """仅发现 Ceph 集群已分配给 OSD 的块设备。"""

    run = subprocess.run if runner is None else runner
    # 原因：OSD 元数据将 SMART 访问和上报范围限制在 Ceph 管理的介质内。
    proc = run(
        [ceph_bin, "osd", "metadata", "-f", "json"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr or proc.stdout or "ceph osd metadata failed")
    try:
        metadata = json.loads(proc.stdout)
    except ValueError as e:
        raise RuntimeError("ceph osd metadata returned invalid JSON") from e
    if not isinstance(metadata, list):
        raise RuntimeError("ceph osd metadata JSON root is not a list")
    return parse_osd_metadata_devices(metadata)


def send_report(
    report: Dict[str, Any],
    ceph_bin: str = "ceph",
    runner: Optional[Callable[..., Any]] = None,
) -> None:
    """通过 Ceph CLI 将一份报告提交给当前活动的 wear MGR 模块。"""

    run = subprocess.run if runner is None else runner
    proc = run(
        [ceph_bin, "wear", "report", "-i", "-"],
        input=json.dumps(report, sort_keys=True),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr or proc.stdout or "ceph wear report failed")


def load_agent_config(path: str) -> Dict[str, Any]:
    """加载并校验 cephadm 生成的 Agent 配置。"""

    with open(path, encoding="utf-8") as f:
        config = json.load(f)
    if not isinstance(config, dict):
        raise ValueError("wear-agent config JSON root must be an object")
    return config


def apply_config(args: argparse.Namespace, defaults: argparse.Namespace, config: Dict[str, Any]) -> None:
    """仅当 CLI 仍为默认值时应用配置文件中的值。"""

    # 原因：显式命令行参数必须优先于 cephadm 生成的配置。
    for key in [
        "ceph_bin",  # Ceph CLI 可执行文件路径。
        "host",  # 写入报告的主机名。
        "interval",  # 两次采样之间的时间，单位：秒。
        "diskstats",  # 主机 /proc/diskstats 的可访问路径。
        "bucket_size",  # eBPF 逻辑热点桶大小，单位：字节。
        "top_hotspots",  # 每个设备每次上报保留的最热桶数量。
        "once",  # 是否只生成一份增量报告后退出。
        "no_bpf",  # 是否禁用 eBPF 热点采集。
    ]:
        if key in config and getattr(args, key) == getattr(defaults, key):
            setattr(args, key, config[key])


class DiskstatsCollector:
    """读取内核块设备累计写入量，用于计算采样周期增量。"""

    def __init__(self, path: str = "/proc/diskstats") -> None:
        """选择 diskstats 数据源，并允许测试注入替代文件。"""

        self.path = path

    def sample(self) -> Dict[str, DiskStat]:
        """读取并解析当前累计 diskstats 快照。"""

        with open(self.path, encoding="utf-8") as f:
            return self.parse(f)

    @staticmethod
    def parse(lines: Iterable[str]) -> Dict[str, DiskStat]:
        """解析合法块设备记录，并跳过不支持或格式错误的行。"""

        stats = {}
        for line in lines:
            parts = line.split()
            if len(parts) < 10:
                continue
            try:
                stat = DiskStat(
                    major=int(parts[0]),
                    minor=int(parts[1]),
                    name=parts[2],
                    writes_completed=int(parts[7]),
                    sectors_written=int(parts[9]),
                )
            except ValueError:
                continue
            stats[stat.name] = stat
        return stats


def diskstats_delta(
    previous: DiskStat,
    current: DiskStat,
    interval_seconds: float,
    sector_size: int = DEFAULT_SECTOR_SIZE,
) -> WriteDelta:
    """计算非负写入增量及采样周期写入速率。"""

    if previous.name != current.name:
        raise ValueError("cannot diff diskstats for different devices")
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")

    # 原因：设备重新绑定或主机重启会重置计数器，重置不能产生负写入量，
    # 否则会破坏寿命估算。
    sectors_delta = max(0, current.sectors_written - previous.sectors_written)
    ops_delta = max(0, current.writes_completed - previous.writes_completed)
    bytes_delta = sectors_delta * sector_size
    return WriteDelta(
        write_bytes=bytes_delta,
        write_ops=ops_delta,
        write_rate_bps=bytes_delta / interval_seconds,
    )


class HotspotTracker:
    """将逻辑写事件聚合到固定大小的设备区域。"""

    def __init__(self, bucket_size: int = DEFAULT_BUCKET_SIZE) -> None:
        """使用指定逻辑区域大小初始化周期热点桶。"""

        if bucket_size <= 0:
            raise ValueError("bucket_size must be positive")
        self.bucket_size = bucket_size
        # 结构：设备键 -> 桶起始字节偏移 -> {bytes: 周期字节数, ops: 周期操作数}。
        self._buckets: Dict[str, Dict[int, Dict[str, int]]] = {}

    def record(self, dev: str, offset: int, length: int) -> None:
        """将一次写入计入其覆盖到的每个逻辑区域。"""

        if not dev:
            raise ValueError("dev must be non-empty")
        if offset < 0:
            raise ValueError("offset must be non-negative")
        if length <= 0:
            return

        end = offset + length
        # 原因：跨桶请求只按重叠字节计入每个区域，
        # 避免重复计算。
        bucket = offset // self.bucket_size
        while bucket * self.bucket_size < end:
            bucket_start = bucket * self.bucket_size
            bucket_end = bucket_start + self.bucket_size
            overlap = min(end, bucket_end) - max(offset, bucket_start)
            if overlap > 0:
                dev_buckets = self._buckets.setdefault(dev, {})
                entry = dev_buckets.setdefault(bucket_start, {"bytes": 0, "ops": 0})
                entry["bytes"] += overlap
                entry["ops"] += 1
            bucket += 1

    def top(self, dev: str, limit: int = 8) -> List[Dict[str, int]]:
        """按确定性顺序返回写入量最高的热点区域。"""

        if limit <= 0:
            return []
        items = self._buckets.get(dev, {}).items()
        ranked = sorted(items, key=lambda item: (-item[1]["bytes"], item[0]))
        return [
            {
                "offset": offset,  # 热点桶起始逻辑偏移，单位：字节。
                "len": self.bucket_size,  # 热点桶覆盖长度，单位：字节。
                "bytes": data["bytes"],  # 本周期写入该桶的字节数。
                "ops": data["ops"],  # 本周期与该桶相交的写操作数。
            }
            for offset, data in ranked[:limit]
        ]

    def clear(self, dev: Optional[str] = None) -> None:
        """清除指定设备的周期热点，或重置全部热点。"""

        if dev is None:
            self._buckets.clear()
        else:
            self._buckets.pop(dev, None)


EBPF_BLOCK_WRITE_PROGRAM = r"""
#include <linux/blkdev.h>

struct write_event_t {
    u32 dev;       // 内核编码的 dev_t，用于还原主次设备号。
    u64 sector;    // 写请求起始扇区号，逻辑偏移为 sector * 512。
    u32 bytes;     // 本次块层写请求长度，单位：字节。
};

BPF_PERF_OUTPUT(events);

TRACEPOINT_PROBE(block, block_rq_issue)
{
    if (args->rwbs[0] != 'W') {
        return 0;
    }

    struct write_event_t event = {};
    event.dev = args->dev;
    event.sector = args->sector;
    event.bytes = args->nr_sector * 512;
    events.perf_submit(args, &event, sizeof(event));
    return 0;
}
"""


class BPFUnavailable(RuntimeError):
    """表示可选 eBPF 功能无法初始化。"""


class EBPFBlockWriteCollector:
    """将块写 tracepoint 事件送入热点跟踪器。"""

    def __init__(
        self,
        tracker: HotspotTracker,
        bpf_factory: Optional[Callable[..., Any]] = None,
    ) -> None:
        """保存热点跟踪器及可选的可注入 BPF 实现。"""

        self.tracker = tracker
        self.bpf_factory = bpf_factory
        self._bpf = None

    def start(self) -> None:
        """编译 tracepoint 程序并注册事件回调。"""

        factory = self.bpf_factory
        if factory is None:
            try:
                from bcc import BPF  # type: ignore
            except ImportError as e:
                raise BPFUnavailable("python-bcc is required for eBPF collection") from e
            factory = BPF
        self._bpf = factory(text=EBPF_BLOCK_WRITE_PROGRAM)
        self._bpf["events"].open_perf_buffer(self._handle_event)

    def poll(self, timeout_ms: int = 100) -> None:
        """读取待处理内核事件并更新逻辑热点。"""

        if self._bpf is None:
            raise RuntimeError("collector is not started")
        self._bpf.perf_buffer_poll(timeout=timeout_ms)

    def _handle_event(self, cpu: int, data: Any, size: int) -> None:
        """将单个内核事件转换到 diskstats 使用的设备键空间。"""

        if self._bpf is None:
            return
        event = self._bpf["events"].event(data)
        # 原因：tracepoint 的 dev_t 使用内核主次设备号编码，而 diskstats 提供
        # WearAgent 使用的独立十进制主次设备号。
        major = event.dev >> 20
        minor = event.dev & ((1 << 20) - 1)
        self.tracker.record(f"{major}:{minor}", event.sector * DEFAULT_SECTOR_SIZE, event.bytes)


def build_wear_report(
    host: str,
    devid: str,
    dev: str,
    previous: DiskStat,
    current: DiskStat,
    interval_seconds: float,
    smart: Optional[Dict[str, Any]] = None,
    hot_buckets: Optional[List[Dict[str, int]]] = None,
    ts: Optional[int] = None,
) -> Dict[str, Any]:
    """构造 wear MGR 模块消费的稳定 JSON 上报结构。"""

    delta = diskstats_delta(previous, current, interval_seconds)
    return {
        "host": host,  # 采集节点主机名。
        "devid": devid,  # Ceph 稳定设备 ID，用于跨重启关联同一 SSD。
        "dev": dev,  # 当前内核设备名，例如 nvme0n1。
        "ts": int(time()) if ts is None else ts,  # 采样时间，Unix 秒。
        # 以下两个 delta 均来自相邻两次 /proc/diskstats 快照。
        "host_write_bytes_delta": delta.write_bytes,  # 周期写入量，字节。
        "host_write_ops_delta": delta.write_ops,  # 周期写操作数，次。
        "write_rate_bps": delta.write_rate_bps,  # 周期平均写速率，字节/秒。
        "smart": smart or {},  # smartctl -x --json 的原始设备健康数据。
        "hot_buckets": hot_buckets or [],  # eBPF 统计的周期逻辑热点列表。
    }


class WearAgent:
    """协调主机计数器、SMART、热点采集和报告提交。"""

    def __init__(
        self,
        devices: Dict[str, str],
        host: Optional[str] = None,
        diskstats: Optional[DiskstatsCollector] = None,
        hotspots: Optional[HotspotTracker] = None,
        smart_collector: Optional[Callable[[str], Dict[str, Any]]] = None,
        submitter: Optional[Callable[[Dict[str, Any]], None]] = None,
        clock: Callable[[], float] = time,
        top_hotspots: int = 8,
    ) -> None:
        """为明确发现的 Ceph 设备创建采集 Agent。"""

        self.devices = [DeviceRef(dev=dev, devid=devid) for dev, devid in devices.items()]
        self.host = host or socket.gethostname()
        self.diskstats = diskstats or DiskstatsCollector()
        self.hotspots = hotspots or HotspotTracker()
        self.smart_collector = smart_collector or collect_smart
        self.submitter = submitter or send_report
        self.clock = clock
        self.top_hotspots = top_hotspots
        self._last_sample: Optional[Dict[str, DiskStat]] = None
        self._last_ts: Optional[float] = None

    def sample_once(self, submit: bool = True) -> List[Dict[str, Any]]:
        """采集一次快照，并在已有基线时生成报告。"""

        current = self.diskstats.sample()
        now = self.clock()
        if self._last_sample is None or self._last_ts is None:
            # 原因：diskstats 是累计计数器，首次采样只能建立基线，
            # 无法生成周期增量。
            self._last_sample = current
            self._last_ts = now
            return []

        interval = now - self._last_ts
        if interval <= 0:
            return []

        reports = []
        for device in self.devices:
            previous_stat = self._last_sample.get(device.dev)
            current_stat = current.get(device.dev)
            if previous_stat is None or current_stat is None:
                continue

            smart = self._collect_smart(device.dev)
            hot_key = diskstat_key(current_stat)
            hot_buckets = self.hotspots.top(hot_key, self.top_hotspots)
            if not hot_buckets:
                # 原因：注入式采集器和旧调用方可能仍以设备名而非
                # major:minor 记录热点。
                hot_buckets = self.hotspots.top(device.dev, self.top_hotspots)

            report = build_wear_report(
                host=self.host,
                devid=device.devid,
                dev=device.dev,
                previous=previous_stat,
                current=current_stat,
                interval_seconds=interval,
                smart=smart,
                hot_buckets=hot_buckets,
                ts=int(now),
            )
            reports.append(report)
            if submit:
                self.submitter(report)
            # 原因：热点描述当前上报周期而非进程全生命周期，
            # 已消费的桶必须及时清除。
            self.hotspots.clear(hot_key)
            if hot_key != device.dev:
                self.hotspots.clear(device.dev)

        self._last_sample = current
        self._last_ts = now
        return reports

    def _collect_smart(self, dev: str) -> Dict[str, Any]:
        """采集 SMART 数据，同时确保单设备失败不会中断其他报告。"""

        try:
            return self.smart_collector(dev)
        except SmartctlError as e:
            # 原因：单个设备无法读取时仍应保留 diskstats 上报；
            # 报告内的错误也能明确展示 SMART 信号缺失原因。
            return {"smartctl_error": str(e)}


def run_agent(
    agent: WearAgent,
    interval: float = 60.0,
    iterations: Optional[int] = None,
    sleeper: Callable[[float], None] = sleep,
    poller: Optional[Callable[[], None]] = None,
) -> int:
    """按固定周期执行采样，并可限制执行次数。"""

    if interval <= 0:
        raise ValueError("interval must be positive")
    count = 0
    while iterations is None or count < iterations:
        if poller is not None:
            poller()
        agent.sample_once()
        count += 1
        if iterations is not None and count >= iterations:
            break
        sleeper(interval)
    return count


def build_arg_parser() -> argparse.ArgumentParser:
    """定义独立运行和 cephadm 托管共用的命令行参数。"""

    parser = argparse.ArgumentParser(description="Collect SSD wear data and report it to ceph-mgr")
    parser.add_argument("--config", default=None, help="wear-agent JSON config path")
    parser.add_argument("--ceph-bin", default="ceph", help="ceph command path")
    parser.add_argument("--host", default=None, help="host name to include in reports")
    parser.add_argument("--interval", type=float, default=60.0, help="sample interval in seconds")
    parser.add_argument("--diskstats", default="/proc/diskstats", help="diskstats path")
    parser.add_argument("--bucket-size", type=int, default=DEFAULT_BUCKET_SIZE, help="hotspot bucket size")
    parser.add_argument("--top-hotspots", type=int, default=8, help="number of hotspot buckets per report")
    parser.add_argument("--once", action="store_true", help="collect one delta sample and exit")
    parser.add_argument("--no-bpf", action="store_true", help="disable eBPF hotspot collection")
    return parser


def create_agent_from_args(args: argparse.Namespace) -> WearAgent:
    """发现 Ceph 设备并根据参数创建 Agent。"""

    devices = load_ceph_device_map(args.ceph_bin)
    hotspots = HotspotTracker(bucket_size=args.bucket_size)

    def submitter(report: Dict[str, Any]) -> None:
        """使用与设备发现相同的 Ceph 命令提交报告。"""

        send_report(report, ceph_bin=args.ceph_bin)

    return WearAgent(
        devices=devices,
        host=args.host,
        diskstats=DiskstatsCollector(args.diskstats),
        hotspots=hotspots,
        submitter=submitter,
        top_hotspots=args.top_hotspots,
    )


def main(argv: Optional[List[str]] = None) -> int:
    """使用可选 cephadm 配置和 eBPF 增强功能运行 WearAgent。"""

    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if args.config:
        defaults = parser.parse_args([])
        apply_config(args, defaults, load_agent_config(args.config))
    agent = create_agent_from_args(args)
    poller = None
    if not args.no_bpf:
        collector = EBPFBlockWriteCollector(agent.hotspots)
        try:
            collector.start()

            def poll_bpf() -> None:
                """以非阻塞方式读取 eBPF 事件，避免拖延采样循环。"""

                collector.poll(timeout_ms=0)

            poller = poll_bpf
        except BPFUnavailable:
            # 原因：eBPF 热点属于可选增强；缺少 BCC 的主机
            # 仍必须采集 SMART 和 diskstats。
            poller = None
        except Exception as e:
            # 原因：校验器或内核异常应与缺少 BCC 一样降级，
            # 同时保留核心 SMART 和 diskstats 信号。
            LOG.warning("eBPF collection disabled: %s", e)
            poller = None
    # 原因：--once 需要生成一份增量报告，因此必须先建立基线，
    # 再在配置周期后进行第二次采样。
    iterations = 2 if args.once else None
    run_agent(agent, interval=args.interval, iterations=iterations, sleeper=sleep, poller=poller)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
