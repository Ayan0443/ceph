from unittest import mock

from cephadmlib import daemon_form
from cephadmlib.daemon_identity import DaemonIdentity
from cephadmlib.daemons import WearAgent


def test_wear_agent_daemon_form_mounts_config_devices_and_diskstats():
    fsid = 'daeb985e-58c7-11ee-a536-201e8814f771'
    ctx = mock.MagicMock()
    ctx.data_dir = '/var/lib/ceph'
    ident = DaemonIdentity(fsid, 'wear-agent', 'host-a')
    agent = WearAgent(ctx, ident, {'wear-agent.json': '{"interval": 60}'}, image='image')

    mounts = {}
    args = []
    agent.customize_container_mounts(ctx, mounts)
    agent.customize_process_args(ctx, args)

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


def test_wear_agent_daemon_form_is_registered():
    assert daemon_form.choose('wear-agent') is WearAgent
