"""URDF 离线结构与物理参数检查服务（纯后端）。"""

from .issues import Issue, Severity
from .checker import inspect_urdf_bytes, inspect_urdf_file

__all__ = ["Issue", "Severity", "inspect_urdf_bytes", "inspect_urdf_file"]
__version__ = "1.0.0"
