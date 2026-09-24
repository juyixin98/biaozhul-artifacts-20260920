"""自定义 JSON 格式的 pydantic 模型。

SBOM 格式（本服务自定义，刻意保持最小）::

    {
      "sbom_version": "1.0",
      "component": {"name": "app", "version": "1.0.0"},
      "packages": [
        {
          "ecosystem": "npm",            // npm | pypi | maven | ... 自定义小写字符串
          "name": "left-pad",
          "version": "1.3.0",            // 应为 SemVer；非法版本会被标为 unknown
          "is_root": false,              // 可选；省略时按依赖图自动判定根
          "dependencies": ["npm:left-pad@1.3.0"]
        }
      ]
    }

包标识（身份键）为 ``ecosystem:name@version``，因此 **同名不同生态、同名不同版本
都天然是不同节点**，服务不会把它们合并。

漏洞馈送见 feed.py（带分离签名的信封）。
"""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class SBOMComponent(BaseModel):
    name: str = Field(min_length=1)
    version: Optional[str] = None


class SBOMPackage(BaseModel):
    ecosystem: str = Field(min_length=1)
    name: str = Field(min_length=1)
    version: str
    is_root: Optional[bool] = None
    dependencies: List[str] = Field(default_factory=list)

    @property
    def id(self) -> str:
        return make_package_id(self.ecosystem, self.name, self.version)


class SBOM(BaseModel):
    sbom_version: str = "1.0"
    component: Optional[SBOMComponent] = None
    packages: List[SBOMPackage] = Field(default_factory=list)


def make_package_id(ecosystem: str, name: str, version: str) -> str:
    """全局包身份键：生态 + 名称 + 版本。"""
    return f"{ecosystem}:{name}@{version}"
