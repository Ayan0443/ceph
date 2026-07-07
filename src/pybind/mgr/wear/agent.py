"""
Host-side SSD wear collection helpers.

The real WearAgent can feed write events from eBPF into HotspotTracker.  The
same module also provides a /proc/diskstats fallback for environments where BPF
is unavailable or intentionally disabled.
"""

import argparse
import json
import logging
import socket
import subprocess
from dataclasses import dataclass
from time import sleep, time
from typing import Any, Callable, Dict, Iterable, List, Optional


DEFAULT_SECTOR_SIZE = 512
DEFAULT_BUCKET_SIZE = 1 << 30

LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class DiskStat:
    major: int
    minor: int
    name: str
    writes_completed: int
    sectors_written: int


@dataclass(frozen=True)
class WriteDelta:
    write_bytes: int
    write_ops: int
    write_rate_bps: float


@dataclass(frozen=True)
class DeviceRef:
    dev: str
    devid: str


class SmartctlError(RuntimeError):
    pass


def dev_path(dev: str) -> str:
    return dev if dev.startswith("/dev/") else f"/dev/{dev}"


def collect_smart(dev: str, timeout: int = 30, runner: Optional[Callable[..., Any]] = None) -> Dict[str, Any]:
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
    return f"{stat.major}:{stat.minor}"


def parse_device_ids(device_ids: str) -> Dict[str, str]:
    devices = {}
    for item in device_ids.replace(";", ",").replace(" ", ",").split(","):
        if not item or "=" not in item:
            continue
        dev, devid = item.split("=", 1)
        if dev and devid:
            devices[dev] = devid
    return devices


def parse_osd_metadata_devices(metadata: Iterable[Dict[str, Any]]) -> Dict[str, str]:
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
    run = subprocess.run if runner is None else runner
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
    with open(path, encoding="utf-8") as f:
        config = json.load(f)
    if not isinstance(config, dict):
        raise ValueError("wear-agent config JSON root must be an object")
    return config


def apply_config(args: argparse.Namespace, defaults: argparse.Namespace, config: Dict[str, Any]) -> None:
    for key in [
        "ceph_bin",
        "host",
        "interval",
        "diskstats",
        "bucket_size",
        "top_hotspots",
        "once",
        "no_bpf",
    ]:
        if key in config and getattr(args, key) == getattr(defaults, key):
            setattr(args, key, config[key])


class DiskstatsCollector:
    def __init__(self, path: str = "/proc/diskstats") -> None:
        self.path = path

    def sample(self) -> Dict[str, DiskStat]:
        with open(self.path, encoding="utf-8") as f:
            return self.parse(f)

    @staticmethod
    def parse(lines: Iterable[str]) -> Dict[str, DiskStat]:
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
    if previous.name != current.name:
        raise ValueError("cannot diff diskstats for different devices")
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")

    sectors_delta = max(0, current.sectors_written - previous.sectors_written)
    ops_delta = max(0, current.writes_completed - previous.writes_completed)
    bytes_delta = sectors_delta * sector_size
    return WriteDelta(
        write_bytes=bytes_delta,
        write_ops=ops_delta,
        write_rate_bps=bytes_delta / interval_seconds,
    )


class HotspotTracker:
    def __init__(self, bucket_size: int = DEFAULT_BUCKET_SIZE) -> None:
        if bucket_size <= 0:
            raise ValueError("bucket_size must be positive")
        self.bucket_size = bucket_size
        self._buckets: Dict[str, Dict[int, Dict[str, int]]] = {}

    def record(self, dev: str, offset: int, length: int) -> None:
        if not dev:
            raise ValueError("dev must be non-empty")
        if offset < 0:
            raise ValueError("offset must be non-negative")
        if length <= 0:
            return

        end = offset + length
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
        if limit <= 0:
            return []
        items = self._buckets.get(dev, {}).items()
        ranked = sorted(items, key=lambda item: (-item[1]["bytes"], item[0]))
        return [
            {
                "offset": offset,
                "len": self.bucket_size,
                "bytes": data["bytes"],
                "ops": data["ops"],
            }
            for offset, data in ranked[:limit]
        ]

    def clear(self, dev: Optional[str] = None) -> None:
        if dev is None:
            self._buckets.clear()
        else:
            self._buckets.pop(dev, None)


EBPF_BLOCK_WRITE_PROGRAM = r"""
#include <linux/blkdev.h>

struct write_event_t {
    u32 dev;
    u64 sector;
    u32 bytes;
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
    pass


class EBPFBlockWriteCollector:
    def __init__(
        self,
        tracker: HotspotTracker,
        bpf_factory: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.tracker = tracker
        self.bpf_factory = bpf_factory
        self._bpf = None

    def start(self) -> None:
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
        if self._bpf is None:
            raise RuntimeError("collector is not started")
        self._bpf.perf_buffer_poll(timeout=timeout_ms)

    def _handle_event(self, cpu: int, data: Any, size: int) -> None:
        if self._bpf is None:
            return
        event = self._bpf["events"].event(data)
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
    delta = diskstats_delta(previous, current, interval_seconds)
    return {
        "host": host,
        "devid": devid,
        "dev": dev,
        "ts": int(time()) if ts is None else ts,
        "host_write_bytes_delta": delta.write_bytes,
        "host_write_ops_delta": delta.write_ops,
        "write_rate_bps": delta.write_rate_bps,
        "smart": smart or {},
        "hot_buckets": hot_buckets or [],
    }


class WearAgent:
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
        current = self.diskstats.sample()
        now = self.clock()
        if self._last_sample is None or self._last_ts is None:
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
            self.hotspots.clear(hot_key)
            if hot_key != device.dev:
                self.hotspots.clear(device.dev)

        self._last_sample = current
        self._last_ts = now
        return reports

    def _collect_smart(self, dev: str) -> Dict[str, Any]:
        try:
            return self.smart_collector(dev)
        except SmartctlError as e:
            return {"smartctl_error": str(e)}


def run_agent(
    agent: WearAgent,
    interval: float = 60.0,
    iterations: Optional[int] = None,
    sleeper: Callable[[float], None] = sleep,
    poller: Optional[Callable[[], None]] = None,
) -> int:
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
    devices = load_ceph_device_map(args.ceph_bin)
    hotspots = HotspotTracker(bucket_size=args.bucket_size)

    def submitter(report: Dict[str, Any]) -> None:
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
                collector.poll(timeout_ms=0)

            poller = poll_bpf
        except BPFUnavailable:
            poller = None
        except Exception as e:
            LOG.warning("eBPF collection disabled: %s", e)
            poller = None
    iterations = 2 if args.once else None
    run_agent(agent, interval=args.interval, iterations=iterations, sleeper=sleep, poller=poller)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
