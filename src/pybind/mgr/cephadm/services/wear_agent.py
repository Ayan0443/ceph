import json

from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

from ceph.deployment.service_spec import ServiceSpec

from .cephadmservice import CephadmDaemonDeploySpec, CephService
from .service_registry import register_cephadm_service

if TYPE_CHECKING:
    from ..module import CephadmOrchestrator


@register_cephadm_service
class WearAgentService(CephService):
    TYPE = 'wear-agent'

    DEFAULT_INTERVAL = 60.0
    DEFAULT_BUCKET_SIZE = 1 << 30
    DEFAULT_TOP_HOTSPOTS = 8

    @classmethod
    def get_dependencies(cls, mgr: "CephadmOrchestrator",
                         spec: Optional[ServiceSpec] = None,
                         daemon_type: Optional[str] = None) -> List[str]:
        return []

    def prepare_create(self, daemon_spec: CephadmDaemonDeploySpec) -> CephadmDaemonDeploySpec:
        assert self.TYPE == daemon_spec.daemon_type
        daemon_id, host = daemon_spec.daemon_id, daemon_spec.host

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
        config, deps = super().generate_config(daemon_spec)
        cfg = {
            'ceph_bin': '/usr/bin/ceph',
            'host': daemon_spec.host,
            'interval': self.DEFAULT_INTERVAL,
            'diskstats': '/host/proc/diskstats',
            'bucket_size': self.DEFAULT_BUCKET_SIZE,
            'top_hotspots': self.DEFAULT_TOP_HOTSPOTS,
            'no_bpf': False,
        }
        config['wear-agent.json'] = json.dumps(cfg, sort_keys=True)
        return config, deps
