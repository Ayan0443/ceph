import json

import pytest

from wear.agent import (
    DiskstatsCollector,
    EBPFBlockWriteCollector,
    HotspotTracker,
    SmartctlError,
    WearAgent,
    apply_config,
    build_arg_parser,
    build_wear_report,
    collect_smart,
    diskstats_delta,
    load_agent_config,
    load_ceph_device_map,
    parse_device_ids,
    parse_osd_metadata_devices,
    run_agent,
    send_report,
)
import wear.agent as agent_mod


def test_diskstats_parse_and_delta():
    """验证内核累计计数会转换为周期字节数、操作数和速率。"""

    previous = DiskstatsCollector.parse([
        "259 0 nvme0n1 0 0 0 0 10 0 20 0 0 0 0 0 0 0 0",
    ])
    current = DiskstatsCollector.parse([
        "259 0 nvme0n1 0 0 0 0 15 0 28 0 0 0 0 0 0 0 0",
    ])

    delta = diskstats_delta(previous["nvme0n1"], current["nvme0n1"], 2.0)

    assert delta.write_ops == 5
    assert delta.write_bytes == 8 * 512
    assert delta.write_rate_bps == pytest.approx(2048.0)


def test_hotspot_tracker_splits_cross_bucket_writes():
    """验证跨区域写入会按重叠范围分配到每个逻辑桶。"""

    tracker = HotspotTracker(bucket_size=100)

    tracker.record("nvme0n1", 50, 175)
    tracker.record("nvme0n1", 120, 20)

    assert tracker.top("nvme0n1") == [
        {"offset": 100, "len": 100, "bytes": 120, "ops": 2},
        {"offset": 0, "len": 100, "bytes": 50, "ops": 1},
        {"offset": 200, "len": 100, "bytes": 25, "ops": 1},
    ]


def test_build_wear_report_matches_mgr_report_contract():
    """验证主机报告结构和字段单位符合 MGR 契约。"""

    previous = DiskstatsCollector.parse([
        "259 0 nvme0n1 0 0 0 0 10 0 20 0 0 0 0 0 0 0 0",
    ])["nvme0n1"]
    current = DiskstatsCollector.parse([
        "259 0 nvme0n1 0 0 0 0 12 0 30 0 0 0 0 0 0 0 0",
    ])["nvme0n1"]

    report = build_wear_report(
        host="host-a",
        devid="nvme-SERIAL",
        dev="nvme0n1",
        previous=previous,
        current=current,
        interval_seconds=5.0,
        smart={"percentage_used": 7},
        hot_buckets=[{"offset": 0, "len": 100, "bytes": 5120, "ops": 2}],
        ts=123,
    )

    assert report == {
        "host": "host-a",
        "devid": "nvme-SERIAL",
        "dev": "nvme0n1",
        "ts": 123,
        "host_write_bytes_delta": 10 * 512,
        "host_write_ops_delta": 2,
        "write_rate_bps": 1024.0,
        "smart": {"percentage_used": 7},
        "hot_buckets": [{"offset": 0, "len": 100, "bytes": 5120, "ops": 2}],
    }


def test_collect_smart_runs_smartctl_json():
    """验证 SMART 采集使用只读 JSON 输出和指定超时时间。"""

    calls = []

    class Proc:
        """模拟成功的 smartctl 子进程结果。"""

        returncode = 0
        stdout = '{"percentage_used": 4}'
        stderr = ""

    def runner(cmd, **kwargs):
        """记录 smartctl 参数并返回成功的模拟进程。"""

        calls.append((cmd, kwargs))
        return Proc()

    assert collect_smart("nvme0n1", timeout=7, runner=runner) == {"percentage_used": 4}
    assert calls[0][0] == ["smartctl", "-x", "--json", "/dev/nvme0n1"]
    assert calls[0][1]["timeout"] == 7
    assert calls[0][1]["check"] is False


def test_collect_smart_reports_smartctl_errors():
    """验证 smartctl 失败会转换为 Agent 专用异常。"""

    class Proc:
        """模拟失败的 smartctl 子进程结果。"""

        returncode = 2
        stdout = ""
        stderr = "permission denied"

    with pytest.raises(SmartctlError, match="permission denied"):
        collect_smart("/dev/nvme0n1", runner=lambda *args, **kwargs: Proc())


def test_parse_device_ids_and_osd_metadata_devices():
    """验证合法 Ceph 设备 ID 会被合并，错误元数据会被跳过。"""

    assert parse_device_ids("nvme0n1=ID0,sdb=ID1;bad,empty=") == {
        "nvme0n1": "ID0",
        "sdb": "ID1",
    }
    assert parse_osd_metadata_devices([
        {"id": 0, "device_ids": "nvme0n1=ID0"},
        {"id": 1, "device_ids": "sdb=ID1"},
        {"id": 2, "device_ids": None},
    ]) == {"nvme0n1": "ID0", "sdb": "ID1"}


def test_load_ceph_device_map_runs_ceph_metadata_command():
    """验证设备发现使用 OSD 元数据并解析设备归属映射。"""

    calls = []

    class Proc:
        """模拟成功的 ceph osd metadata 子进程结果。"""

        returncode = 0
        stdout = '[{"device_ids": "nvme0n1=ID0"}]'
        stderr = ""

    def runner(cmd, **kwargs):
        """记录 Ceph 发现命令并返回元数据 JSON。"""

        calls.append((cmd, kwargs))
        return Proc()

    assert load_ceph_device_map(ceph_bin="ceph-test", runner=runner) == {"nvme0n1": "ID0"}
    assert calls[0][0] == ["ceph-test", "osd", "metadata", "-f", "json"]
    assert calls[0][1]["check"] is False


def test_send_report_posts_json_to_wear_report_command():
    """验证报告会序列化到 wear report 命令的标准输入。"""

    calls = []

    class Proc:
        """模拟成功的 ceph wear report 子进程结果。"""

        returncode = 0
        stdout = "{}"
        stderr = ""

    def runner(cmd, **kwargs):
        """记录上报命令及其序列化标准输入。"""

        calls.append((cmd, kwargs))
        return Proc()

    send_report({"devid": "ID0", "host": "host-a"}, ceph_bin="ceph-test", runner=runner)

    assert calls[0][0] == ["ceph-test", "wear", "report", "-i", "-"]
    assert calls[0][1]["input"] == '{"devid": "ID0", "host": "host-a"}'
    assert calls[0][1]["text"] is True


class FakeDiskstats:
    """为 Agent 测试提供确定性的 diskstats 快照。"""

    def __init__(self, samples):
        """按照 Agent 应观测的顺序保存快照。"""

        self.samples = list(samples)

    def sample(self):
        """返回下一份累计计数快照。"""

        return self.samples.pop(0)


def test_ebpf_collector_records_kernel_write_events_as_hotspots():
    """验证内核 dev_t 和扇区字段会转换为预期热点。"""

    tracker = HotspotTracker(bucket_size=1024)

    class FakeEvent:
        """模拟一次块写 tracepoint 事件。"""

        dev = (259 << 20) | 0
        sector = 2
        bytes = 1024

    class FakeEvents:
        """模拟采集器使用的 BCC perf 事件表。"""

        def open_perf_buffer(self, callback):
            """保存采集器注册的回调。"""

            self.callback = callback

        def event(self, data):
            """将模拟 perf 数据解码为预定义事件。"""

            return FakeEvent()

    class FakeBPF:
        """模拟 WearAgent 使用的最小 BCC 接口。"""

        def __init__(self, text):
            """记录编译程序并提供事件表。"""

            self.text = text
            self.events = FakeEvents()

        def __getitem__(self, key):
            """返回模拟 perf 事件表。"""

            assert key == "events"
            return self.events

        def perf_buffer_poll(self, timeout):
            """记录采集器使用的轮询超时时间。"""

            self.timeout = timeout

    created = {}

    def bpf_factory(text):
        """创建并保存注入的 BPF 实现。"""

        created["bpf"] = FakeBPF(text)
        return created["bpf"]

    collector = EBPFBlockWriteCollector(tracker, bpf_factory=bpf_factory)

    collector.start()
    collector._handle_event(cpu=0, data=b"event", size=0)
    collector.poll(timeout_ms=3)

    assert created["bpf"].text
    assert created["bpf"].timeout == 3
    assert tracker.top("259:0") == [
        {"offset": 1024, "len": 1024, "bytes": 1024, "ops": 1},
    ]


def test_wear_agent_sample_once_builds_and_submits_reports():
    """验证基线、增量、SMART、热点、提交和周期清理流程。"""

    previous = DiskstatsCollector.parse([
        "259 0 nvme0n1 0 0 0 0 10 0 20 0 0 0 0 0 0 0 0",
    ])
    current = DiskstatsCollector.parse([
        "259 0 nvme0n1 0 0 0 0 12 0 30 0 0 0 0 0 0 0 0",
    ])
    hotspots = HotspotTracker(bucket_size=100)
    hotspots.record("259:0", 0, 75)
    submitted = []
    clock_values = iter([100.0, 105.0])
    agent = WearAgent(
        devices={"nvme0n1": "ID0"},
        host="host-a",
        diskstats=FakeDiskstats([previous, current]),
        hotspots=hotspots,
        smart_collector=lambda dev: {"percentage_used": 8, "dev": dev},
        submitter=submitted.append,
        clock=lambda: next(clock_values),
        top_hotspots=4,
    )

    assert agent.sample_once() == []
    reports = agent.sample_once()

    assert reports == submitted
    assert reports[0]["host"] == "host-a"
    assert reports[0]["devid"] == "ID0"
    assert reports[0]["dev"] == "nvme0n1"
    assert reports[0]["host_write_bytes_delta"] == 10 * 512
    assert reports[0]["host_write_ops_delta"] == 2
    assert reports[0]["write_rate_bps"] == 1024.0
    assert reports[0]["smart"] == {"percentage_used": 8, "dev": "nvme0n1"}
    assert reports[0]["hot_buckets"] == [
        {"offset": 0, "len": 100, "bytes": 75, "ops": 1},
    ]
    assert hotspots.top("259:0") == []


def test_wear_agent_keeps_running_when_smart_fails():
    """验证单设备 SMART 失败后 diskstats 上报仍会继续。"""

    previous = DiskstatsCollector.parse([
        "259 0 nvme0n1 0 0 0 0 10 0 20 0 0 0 0 0 0 0 0",
    ])
    current = DiskstatsCollector.parse([
        "259 0 nvme0n1 0 0 0 0 11 0 22 0 0 0 0 0 0 0 0",
    ])
    clock_values = iter([1.0, 2.0])

    def smart_collector(dev):
        """模拟无法读取 SMART 数据的设备。"""

        raise SmartctlError("boom")

    agent = WearAgent(
        devices={"nvme0n1": "ID0"},
        diskstats=FakeDiskstats([previous, current]),
        smart_collector=smart_collector,
        submitter=lambda report: None,
        clock=lambda: next(clock_values),
    )

    agent.sample_once()
    report = agent.sample_once()[0]

    assert report["smart"] == {"smartctl_error": "boom"}


def test_load_agent_config_applies_defaults_and_preserves_cli_overrides(tmp_path):
    """验证配置只填充默认值，不覆盖显式 CLI 参数。"""

    config_path = tmp_path / "wear-agent.json"
    config_path.write_text(json.dumps({
        "ceph_bin": "/usr/bin/ceph",
        "host": "host-from-config",
        "interval": 60.0,
        "diskstats": "/host/proc/diskstats",
        "bucket_size": 4096,
        "top_hotspots": 4,
        "no_bpf": True,
    }))
    parser = build_arg_parser()
    args = parser.parse_args(["--config", str(config_path), "--interval", "5"])
    defaults = parser.parse_args([])

    apply_config(args, defaults, load_agent_config(str(config_path)))

    assert args.ceph_bin == "/usr/bin/ceph"
    assert args.host == "host-from-config"
    assert args.interval == 5.0
    assert args.diskstats == "/host/proc/diskstats"
    assert args.bucket_size == 4096
    assert args.top_hotspots == 4
    assert args.no_bpf is True


def test_build_arg_parser_accepts_agent_options():
    """验证全部独立运行和 cephadm 运行参数均可解析。"""

    args = build_arg_parser().parse_args([
        "--ceph-bin", "ceph-test",
        "--host", "host-a",
        "--interval", "5",
        "--diskstats", "/tmp/diskstats",
        "--bucket-size", "4096",
        "--top-hotspots", "3",
        "--once",
        "--no-bpf",
    ])

    assert args.ceph_bin == "ceph-test"
    assert args.host == "host-a"
    assert args.interval == 5.0
    assert args.diskstats == "/tmp/diskstats"
    assert args.bucket_size == 4096
    assert args.top_hotspots == 3
    assert args.once is True
    assert args.no_bpf is True


def test_run_agent_honors_iterations_and_sleeps_between_samples():
    """验证有限循环会按次数轮询，并只在采样之间休眠。"""

    class FakeAgent:
        """统计运行循环发起的采样次数。"""

        def __init__(self):
            """初始化可观测的采样计数。"""

            self.samples = 0

        def sample_once(self):
            """记录一次循环采样且不生成报告。"""

            self.samples += 1
            return []

    polls = []
    sleeps = []
    fake = FakeAgent()

    count = run_agent(
        fake,
        interval=2.5,
        iterations=3,
        sleeper=sleeps.append,
        poller=lambda: polls.append("poll"),
    )

    assert count == 3
    assert fake.samples == 3
    assert polls == ["poll", "poll", "poll"]
    assert sleeps == [2.5, 2.5]


def test_main_once_assembles_agent_and_submits_report(monkeypatch):
    """验证 CLI 单次模式先建立基线，再提交一份真实增量报告。"""

    previous = DiskstatsCollector.parse([
        "259 0 nvme0n1 0 0 0 0 10 0 20 0 0 0 0 0 0 0 0",
    ])
    current = DiskstatsCollector.parse([
        "259 0 nvme0n1 0 0 0 0 12 0 30 0 0 0 0 0 0 0 0",
    ])
    submitted = []
    samples = iter([previous, current])

    monkeypatch.setattr(agent_mod, "load_ceph_device_map", lambda ceph_bin: {"nvme0n1": "ID0"})
    monkeypatch.setattr(agent_mod, "send_report", lambda report, ceph_bin: submitted.append((ceph_bin, report)))
    monkeypatch.setattr(agent_mod, "collect_smart", lambda dev: {"percentage_used": 6})
    monkeypatch.setattr(agent_mod, "sleep", lambda interval: None)
    monkeypatch.setattr(agent_mod.DiskstatsCollector, "sample", lambda self: next(samples))

    assert agent_mod.main([
        "--ceph-bin", "ceph-test",
        "--host", "host-a",
        "--interval", "1",
        "--once",
        "--no-bpf",
    ]) == 0

    assert submitted[0][0] == "ceph-test"
    assert submitted[0][1]["host"] == "host-a"
    assert submitted[0][1]["devid"] == "ID0"
    assert submitted[0][1]["host_write_bytes_delta"] == 10 * 512
    assert submitted[0][1]["smart"] == {"percentage_used": 6}
