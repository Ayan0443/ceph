import os

from unittest import mock

from cephadmlib import daemon_form
from cephadmlib.daemon_identity import DaemonIdentity
from cephadmlib.daemons import WearAgent


def test_wear_agent_daemon_form_mounts_config_devices_and_diskstats():
    """验证容器获得所需路径、参数和 CephX 身份。"""

    fsid = 'daeb985e-58c7-11ee-a536-201e8814f771'
    ctx = mock.MagicMock()
    ctx.data_dir = '/var/lib/ceph'
    ident = DaemonIdentity(fsid, 'wear-agent', 'host-a')
    agent = WearAgent(ctx, ident, {'wear-agent.json': '{"interval": 60}'}, image='image')

    mounts = {}
    args = []
    envs = []
    agent.customize_container_mounts(ctx, mounts)
    agent.customize_process_args(ctx, args)
    agent.customize_container_envs(ctx, envs)

    data_dir = '/var/lib/ceph/%s/wear-agent.host-a' % fsid
    assert mounts[data_dir + '/config'] == '/etc/ceph/ceph.conf:z'
    assert mounts[data_dir + '/keyring'] == '/etc/ceph/keyring:z'
    assert mounts[data_dir + '/wear-agent.json'] == '/usr/share/ceph/wear-agent.json:z'
    assert mounts['/dev'] == '/dev'
    assert mounts['/proc/diskstats'] == '/host/proc/diskstats:ro'
    assert args == [
        '/usr/share/ceph/mgr/wear/agent.py',
        '--config',
        '/usr/share/ceph/wear-agent.json',
    ]
    assert envs == [
        'CEPH_ARGS=--name client.wear-agent.host-a --keyring /etc/ceph/keyring'
    ]


def test_wear_agent_daemon_form_is_registered():
    """验证 cephadm 会将 wear-agent 部署分发给该 daemon form。"""

    assert daemon_form.choose('wear-agent') is WearAgent


def test_wear_agent_prepare_data_dir_writes_config(tmp_path):
    """验证通用部署钩子会生成 wear-agent.json。"""

    fsid = 'daeb985e-58c7-11ee-a536-201e8814f771'
    ctx = mock.MagicMock()
    ident = DaemonIdentity(fsid, 'wear-agent', 'host-a')
    agent = WearAgent(ctx, ident, {'wear-agent.json': '{"interval": 60}'}, image='image')

    agent.prepare_data_dir(str(tmp_path), os.getuid(), os.getgid())

    assert (tmp_path / 'wear-agent.json').read_text() == '{"interval": 60}'
