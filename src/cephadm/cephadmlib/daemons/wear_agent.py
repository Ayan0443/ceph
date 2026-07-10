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
    """定义主机侧 SSD 磨损采集容器。"""

    daemon_type = 'wear-agent'
    entrypoint = '/usr/bin/python3'
    agent_path = '/usr/share/ceph/mgr/wear/agent.py'
    config_path = '/usr/share/ceph/wear-agent.json'
    required_files = ['wear-agent.json']

    @classmethod
    def for_daemon_type(cls, daemon_type: str) -> bool:
        """判断该 daemon form 是否处理指定类型。"""

        return cls.daemon_type == daemon_type

    def __init__(
        self,
        ctx: CephadmContext,
        ident: DaemonIdentity,
        config_json: Dict,
        image: str = DEFAULT_IMAGE,
    ):
        """根据 cephadm 部署数据初始化并校验 daemon form。"""

        self.ctx = ctx
        self._identity = ident
        self.image = image
        # 原因：共享配置 JSON 中只保留 Agent 自有文件，
        # 避免将其他 daemon 配置写入该数据目录。
        config = dict_get(config_json, 'wear-agent.json', {})
        self.files = {'wear-agent.json': config}
        self.validate()

    @classmethod
    def init(
        cls, ctx: CephadmContext, fsid: str, daemon_id: str
    ) -> 'WearAgent':
        """兼容旧式 fsid/id 调用形式并创建 daemon form。"""

        return cls.create(
            ctx, DaemonIdentity(fsid, cls.daemon_type, daemon_id)
        )

    @classmethod
    def create(
        cls, ctx: CephadmContext, ident: DaemonIdentity
    ) -> 'WearAgent':
        """读取部署输入并创建 daemon form。"""

        return cls(ctx, ident, fetch_configs(ctx), ctx.image)

    @property
    def identity(self) -> DaemonIdentity:
        """返回规范的集群与 daemon 身份。"""

        return self._identity

    @property
    def fsid(self) -> str:
        """返回用于数据路径和命名的集群 FSID。"""

        return self._identity.fsid

    @property
    def daemon_id(self) -> str:
        """返回按主机划分的 daemon 标识。"""

        return self._identity.daemon_id

    def customize_container_mounts(
        self, ctx: CephadmContext, mounts: Dict[str, str]
    ) -> None:
        """挂载凭据、Agent 配置、设备节点和主机计数器。"""

        data_dir = self.identity.data_dir(ctx.data_dir)
        mounts.update({
            os.path.join(data_dir, 'config'): '/etc/ceph/ceph.conf:z',
            os.path.join(data_dir, 'keyring'): '/etc/ceph/keyring:z',
            os.path.join(data_dir, 'wear-agent.json'): f'{self.config_path}:z',
            # 原因：smartctl 需要真实主机设备节点，
            # 而 diskstats 只需只读挂载主机文件。
            '/dev': '/dev',
            '/proc/diskstats': '/host/proc/diskstats:ro',
        })

    def customize_process_args(
        self, ctx: CephadmContext, args: List[str]
    ) -> None:
        """使用生成的配置运行镜像内置 Python 采集器。"""

        args.extend([self.agent_path, '--config', self.config_path])

    def customize_container_envs(
        self, ctx: CephadmContext, envs: List[str]
    ) -> None:
        """为 CLI 子进程选择 daemon 专用 CephX 身份。"""

        # 原因：ceph 默认使用 client.admin；显式匹配已挂载 keyring
        # 可避免认证失败和权限过大。
        envs.append(
            'CEPH_ARGS=--name client.wear-agent.%s --keyring /etc/ceph/keyring'
            % self.daemon_id
        )

    def validate(self) -> None:
        """拒绝不完整的身份、镜像或部署数据。"""

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
        """返回稳定的 type.id 格式 daemon 名称。"""

        return '%s.%s' % (self.daemon_type, self.daemon_id)

    def get_container_name(self, desc: Optional[str] = None) -> str:
        """返回集群内唯一的容器名，并支持可选后缀。"""

        cname = 'ceph-%s-%s' % (self.fsid, self.get_daemon_name())
        if desc:
            cname = '%s-%s' % (cname, desc)
        return cname

    def create_daemon_dirs(self, data_dir: str, uid: int, gid: int) -> None:
        """向已经创建的 daemon 数据目录写入生成文件。"""

        if not os.path.isdir(data_dir):
            raise OSError('data_dir is not a directory: %s' % (data_dir))
        logger.info('Writing wear-agent config...')
        populate_files(data_dir, self.files, uid, gid)

    def prepare_data_dir(self, data_dir: str, uid: int, gid: int) -> None:
        """通过 cephadm 通用部署钩子准备数据目录。"""

        # 原因：通用部署流程调用 prepare_data_dir；在此委托可确保
        # 容器启动前 wear-agent.json 已存在。
        self.create_daemon_dirs(data_dir, uid, gid)

    def container(self, ctx: CephadmContext) -> CephContainer:
        """创建具备所需主机可见性的部署容器。"""

        # 原因：SMART ioctl 和 eBPF tracepoint 需要
        # 普通无根容器无法获得的权限。
        ctr = daemon_to_container(ctx, self, privileged=True)
        return to_deployment_container(ctx, ctr)

    def config_and_keyring(
        self, ctx: CephadmContext
    ) -> Tuple[Optional[str], Optional[str]]:
        """读取集群配置和最小权限 daemon keyring。"""

        return get_config_and_keyring(ctx)

    def uid_gid(self, ctx: CephadmContext) -> Tuple[int, int]:
        """解析 cephadm 写入 daemon 文件时使用的属主。"""

        return extract_uid_gid(ctx)

    def default_entrypoint(self) -> str:
        """返回 Python 入口，因为采集器以模块文件形式提供。"""

        return self.entrypoint
