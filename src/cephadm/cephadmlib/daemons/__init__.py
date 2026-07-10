from .ceph import Ceph, OSD, CephExporter
from .custom import CustomContainer
from .ingress import HAproxy, Keepalived
from .iscsi import CephIscsi
from .monitoring import Monitoring
from .nfs import NFSGanesha
from .nvmeof import CephNvmeof
from .smb import SMB
from .snmp import SNMPGateway
from .tracing import Tracing
from .node_proxy import NodeProxy
# 原因：导入该类会执行 cephadm 的 daemon form 注册。
from .wear_agent import WearAgent
from .mgmt_gateway import MgmtGateway
from .oauth2_proxy import OAuth2Proxy

__all__ = [
    'Ceph',
    'CephExporter',
    'CephIscsi',
    'CephNvmeof',
    'CustomContainer',
    'HAproxy',
    'Keepalived',
    'Monitoring',
    'NFSGanesha',
    'OSD',
    'SMB',
    'SNMPGateway',
    'Tracing',
    'NodeProxy',
    # 原因：允许从 cephadmlib.daemons 显式导入 WearAgent。
    'WearAgent',
    'MgmtGateway',
    'OAuth2Proxy',
]
