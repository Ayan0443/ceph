import json
from unittest.mock import MagicMock, call

from ceph.deployment.service_spec import PlacementSpec, ServiceSpec, WearAgentSpec
from cephadm.services.cephadmservice import CephadmDaemonDeploySpec
from cephadm.services.wear_agent import WearAgentService
from orchestrator._interface import daemon_type_to_service, service_to_daemon_types


class FakeMgr:
    """提供 WearAgentService 所需的最小 MGR 行为。"""

    def __init__(self):
        """准备可观测日志和 monitor 命令响应。"""

        self.log = MagicMock()
        self.mon_command = MagicMock(return_value=(
            0,
            '[client.wear-agent.host-a]\n    key = secret\n',
            '',
        ))

    def get_minimal_ceph_conf(self):
        """返回嵌入部署数据的最小集群配置。"""

        return '[global]\nfsid = fsid\n'


def test_wear_agent_service_spec_and_orchestrator_mapping():
    """验证通用规格和 orchestrator 映射能识别 wear-agent。"""

    spec = ServiceSpec(service_type='wear-agent', placement=PlacementSpec(host_pattern='*'))

    assert isinstance(spec, WearAgentSpec)
    assert spec.service_name() == 'wear-agent'
    assert daemon_type_to_service('wear-agent') == 'wear-agent'
    assert service_to_daemon_types('wear-agent') == ['wear-agent']


def test_wear_agent_prepare_create_generates_keyring_and_config():
    """验证部署生成最小权限 keyring 和预期运行配置。"""

    mgr = FakeMgr()
    service = WearAgentService(mgr)
    daemon_spec = CephadmDaemonDeploySpec(
        host='host-a',
        daemon_id='host-a',
        service_name='wear-agent',
    )

    prepared = service.prepare_create(daemon_spec)

    expected_caps = [
        'mon', 'allow r, allow command "osd metadata"',
        'mgr', 'allow command "wear report"',
    ]
    mgr.mon_command.assert_called_once_with({
        'prefix': 'auth get-or-create',
        'entity': 'client.wear-agent.host-a',
        'caps': expected_caps,
    })
    assert prepared.keyring == '[client.wear-agent.host-a]\nkey = secret\n'
    assert prepared.final_config['config'] == '[global]\nfsid = fsid\n'
    assert prepared.final_config['keyring'] == prepared.keyring

    cfg = json.loads(prepared.final_config['wear-agent.json'])
    assert cfg == {
        'ceph_bin': '/usr/bin/ceph',
        'host': 'host-a',
        'interval': 60.0,
        'diskstats': '/host/proc/diskstats',
        'bucket_size': 1 << 30,
        'top_hotspots': 8,
        'no_bpf': False,
    }


def test_wear_agent_auth_caps_are_refreshed_when_get_or_create_returns_error():
    """验证已有 CephX 实体的旧权限会被修正为 wear-agent 权限。"""

    mgr = FakeMgr()
    mgr.mon_command = MagicMock(side_effect=[
        (1, '', 'old caps'),
        (0, '', ''),
        (0, '[client.wear-agent.host-a]\n    key = secret\n', ''),
    ])
    service = WearAgentService(mgr)
    daemon_spec = CephadmDaemonDeploySpec(
        host='host-a',
        daemon_id='host-a',
        service_name='wear-agent',
    )

    service.prepare_create(daemon_spec)

    expected_caps = [
        'mon', 'allow r, allow command "osd metadata"',
        'mgr', 'allow command "wear report"',
    ]
    assert call({
        'prefix': 'auth caps',
        'entity': 'client.wear-agent.host-a',
        'caps': expected_caps,
    }) in mgr.mon_command.mock_calls
