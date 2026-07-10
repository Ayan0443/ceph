import json

from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

from ceph.deployment.service_spec import ServiceSpec

from .cephadmservice import CephadmDaemonDeploySpec, CephService
from .service_registry import register_cephadm_service

if TYPE_CHECKING:
    from ..module import CephadmOrchestrator


@register_cephadm_service
class WearAgentService(CephService):
    """为 cephadm 准备按主机部署的 SSD 磨损采集器。"""

    TYPE = 'wear-agent'

    DEFAULT_INTERVAL = 60.0
    DEFAULT_BUCKET_SIZE = 1 << 30
    DEFAULT_TOP_HOTSPOTS = 8

    @classmethod
    def get_dependencies(cls, mgr: "CephadmOrchestrator",
                         spec: Optional[ServiceSpec] = None,
                         daemon_type: Optional[str] = None) -> List[str]:
        """返回空依赖，因为采集工作完全在本机执行。"""

        return []

    def prepare_create(self, daemon_spec: CephadmDaemonDeploySpec) -> CephadmDaemonDeploySpec:
        """附加最小权限凭据和生成的 Agent 配置。"""

        assert self.TYPE == daemon_spec.daemon_type
        daemon_id, host = daemon_spec.daemon_id, daemon_spec.host

        # 原因：Agent 只发现 OSD 设备并提交磨损报告，
        # 无需更大的 monitor 或 MGR 权限。
        keyring = self.get_keyring_with_caps(
            self.get_auth_entity(daemon_id, host=host),
            [
                'mon', 'allow r, allow command "osd metadata"',
                'mgr', 'allow command "wear report"',
            ],
        )
        daemon_spec.keyring = keyring
        daemon_spec.final_config, daemon_spec.deps = self.generate_config(daemon_spec)
        return daemon_spec

    def generate_config(self, daemon_spec: CephadmDaemonDeploySpec) -> Tuple[Dict[str, Any], List[str]]:
        """生成 Agent 容器内消费的运行时 JSON 配置。"""

        config, deps = super().generate_config(daemon_spec)
        # 原因：容器通过只读绑定挂载读取主机计数器；
        # eBPF 默认启用，并在运行时降级。
        cfg = {
            'ceph_bin': '/usr/bin/ceph',  # 容器内 Ceph CLI 路径。
            'host': daemon_spec.host,  # 报告中使用的采集节点主机名。
            'interval': self.DEFAULT_INTERVAL,  # 采样周期，单位：秒。
            'diskstats': '/host/proc/diskstats',  # 主机累计块设备计数文件。
            'bucket_size': self.DEFAULT_BUCKET_SIZE,  # 热点桶大小，字节。
            'top_hotspots': self.DEFAULT_TOP_HOTSPOTS,  # 上报热点桶数量。
            'no_bpf': False,  # False 表示默认尝试启用 eBPF 热点采集。
        }
        config['wear-agent.json'] = json.dumps(cfg, sort_keys=True)
        return config, deps
