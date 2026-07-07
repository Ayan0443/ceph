import sys
import types


class _Base:
    def __init__(self, *args, **kwargs):
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
    @staticmethod
    def to_argdesc(*args, **kwargs):
        return ""


ceph_argparse.CephArgtype = _CephArgtype
sys.modules.setdefault("ceph_argparse", ceph_argparse)
