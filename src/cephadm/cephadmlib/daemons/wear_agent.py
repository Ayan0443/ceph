import logging
import os

from typing import Dict, List, Optional, Tuple

from ..constants import DEFAULT_IMAGE
from ..container_daemon_form import ContainerDaemonForm, daemon_to_container
from ..container_types import CephContainer, extract_uid_gid
from ..context import CephadmContext
from ..context_getters import fetch_configs, get_config_and_keyring
from ..daemon_form import register as register_daemon_form
from ..daemon_identity import DaemonIdentity
from ..data_utils import dict_get, is_fsid
from ..deployment_utils import to_deployment_container
from ..exceptions import Error
from ..file_utils import populate_files

logger = logging.getLogger()


@register_daemon_form
class WearAgent(ContainerDaemonForm):
    """Defines a host-side SSD wear collection container."""

    daemon_type = 'wear-agent'
    entrypoint = '/usr/bin/python3'
    agent_path = '/usr/share/ceph/mgr/wear/agent.py'
    config_path = '/usr/share/ceph/wear-agent.json'
    required_files = ['wear-agent.json']

    @classmethod
    def for_daemon_type(cls, daemon_type: str) -> bool:
        return cls.daemon_type == daemon_type

    def __init__(
        self,
        ctx: CephadmContext,
        ident: DaemonIdentity,
        config_json: Dict,
        image: str = DEFAULT_IMAGE,
    ):
        self.ctx = ctx
        self._identity = ident
        self.image = image
        config = dict_get(config_json, 'wear-agent.json', {})
        self.files = {'wear-agent.json': config}
        self.validate()

    @classmethod
    def init(
        cls, ctx: CephadmContext, fsid: str, daemon_id: str
    ) -> 'WearAgent':
        return cls.create(
            ctx, DaemonIdentity(fsid, cls.daemon_type, daemon_id)
        )

    @classmethod
    def create(
        cls, ctx: CephadmContext, ident: DaemonIdentity
    ) -> 'WearAgent':
        return cls(ctx, ident, fetch_configs(ctx), ctx.image)

    @property
    def identity(self) -> DaemonIdentity:
        return self._identity

    @property
    def fsid(self) -> str:
        return self._identity.fsid

    @property
    def daemon_id(self) -> str:
        return self._identity.daemon_id

    def customize_container_mounts(
        self, ctx: CephadmContext, mounts: Dict[str, str]
    ) -> None:
        data_dir = self.identity.data_dir(ctx.data_dir)
        mounts.update({
            os.path.join(data_dir, 'config'): '/etc/ceph/ceph.conf:z',
            os.path.join(data_dir, 'keyring'): '/etc/ceph/keyring:z',
            os.path.join(data_dir, 'wear-agent.json'): f'{self.config_path}:z',
            '/dev': '/dev',
            '/proc/diskstats': '/host/proc/diskstats:ro',
        })

    def customize_process_args(
        self, ctx: CephadmContext, args: List[str]
    ) -> None:
        args.extend([self.agent_path, '--config', self.config_path])

    def validate(self) -> None:
        if not is_fsid(self.fsid):
            raise Error('not an fsid: %s' % self.fsid)
        if not self.daemon_id:
            raise Error('invalid daemon_id: %s' % self.daemon_id)
        if not self.image:
            raise Error('invalid image: %s' % self.image)
        for fname in self.required_files:
            if fname not in self.files:
                raise Error('required file missing from config-json: %s' % fname)

    def get_daemon_name(self) -> str:
        return '%s.%s' % (self.daemon_type, self.daemon_id)

    def get_container_name(self, desc: Optional[str] = None) -> str:
        cname = 'ceph-%s-%s' % (self.fsid, self.get_daemon_name())
        if desc:
            cname = '%s-%s' % (cname, desc)
        return cname

    def create_daemon_dirs(self, data_dir: str, uid: int, gid: int) -> None:
        if not os.path.isdir(data_dir):
            raise OSError('data_dir is not a directory: %s' % (data_dir))
        logger.info('Writing wear-agent config...')
        populate_files(data_dir, self.files, uid, gid)

    def container(self, ctx: CephadmContext) -> CephContainer:
        ctr = daemon_to_container(ctx, self, privileged=True)
        return to_deployment_container(ctx, ctr)

    def config_and_keyring(
        self, ctx: CephadmContext
    ) -> Tuple[Optional[str], Optional[str]]:
        return get_config_and_keyring(ctx)

    def uid_gid(self, ctx: CephadmContext) -> Tuple[int, int]:
        return extract_uid_gid(ctx)

    def default_entrypoint(self) -> str:
        return self.entrypoint
