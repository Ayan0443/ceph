import sys
import types


class _Base:
    """为单元测试中不可用的 Ceph 扩展类型提供空实现基类。"""

    def __init__(self, *args, **kwargs):
        """接受 MGR 基类可能传入的任意构造参数。"""

        pass


ceph_module = types.ModuleType("ceph_module")
ceph_module.BaseMgrModule = _Base
ceph_module.BaseMgrStandbyModule = _Base
ceph_module.BasePyOSDMap = _Base
ceph_module.BasePyOSDMapIncremental = _Base
ceph_module.BasePyCRUSH = _Base
sys.modules.setdefault("ceph_module", ceph_module)

cephfs = types.ModuleType("cephfs")
cephfs.LibCephFS = _Base
sys.modules.setdefault("cephfs", cephfs)

rados = types.ModuleType("rados")
rados.Rados = _Base
sys.modules.setdefault("rados", rados)

ceph_argparse = types.ModuleType("ceph_argparse")


class _CephArgtype:
    """模拟 Ceph CLI 参数描述依赖。"""

    @staticmethod
    def to_argdesc(*args, **kwargs):
        """返回足以完成命令注册的空参数描述。"""

        return ""


ceph_argparse.CephArgtype = _CephArgtype
sys.modules.setdefault("ceph_argparse", ceph_argparse)
