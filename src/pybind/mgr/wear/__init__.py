# flake8: noqa
import os

try:
    from .module import Module
except ModuleNotFoundError as e:
    if e.name == "ceph_module" and os.environ.get("UNITTEST") == "true":
        Module = None
    else:
        raise
