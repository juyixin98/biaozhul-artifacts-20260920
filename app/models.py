"""请求/响应数据模型（自定义 SBOM JSON 格式与漏洞条目格式）。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class Component(BaseModel):
    """SBOM 中的一个组件。

    - id: SBOM 内唯一标识，用于依赖边引用。
    - name / ecosystem: 包名与生态（如 npm、pypi、maven）。
      同名不同生态视为完全不同的包，绝不合并。
    - version: 完整 SemVer（X.Y.Z，可带预发布/build 元数据）。
      为 null 或无法解析时，该组件的匹配状态为 unknown。
    - dependencies: 直接依赖的组件 id 列表（允许成环）。
    """

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    ecosystem: str = Field(min_length=1)
    version: str | None = None
    dependencies: list[str] = Field(default_factory=list)


class SBOM(BaseModel):
    """自定义 SBOM 文档。"""

    format: str = "sbom-matcher/1"
    name: str | None = None
    components: list[Component] = Field(min_length=1)


class VulnPackage(BaseModel):
    ecosystem: str = Field(min_length=1)
    name: str = Field(min_length=1)


class Vulnerability(BaseModel):
    """一条漏洞记录：作用于某生态某包的一组 SemVer 范围（并集）。"""

    id: str = Field(min_length=1)
    package: VulnPackage
    summary: str = ""
    ranges: list[str] = Field(min_length=1)
    fixed_versions: list[str] = Field(default_factory=list)


class MatchRequest(BaseModel):
    """POST /v1/match 请求体。vulnerabilities 缺省时使用服务内置夹具。"""

    sbom: SBOM
    vulnerabilities: list[Vulnerability] | None = None
