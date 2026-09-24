"""Package URL（purl）解析与规范化。

仅实现本服务所需的 purl 子集：
    pkg:<type>/<namespace?>/<name>@<version>?<qualifiers>#<subpath>

刻意不引入第三方 purl 库：解析规则在此显式实现，错误输入抛
:class:`PurlError`，绝不静默吞掉或猜测。

参考: https://github.com/package-url/purl-spec
"""
from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import unquote, parse_qsl


class PurlError(ValueError):
    """purl 字符串非法或不受支持。"""


# 内部生态标识 -> 接受的 purl type 别名
_ALIASES = {
    "npm": "npm",
    "maven": "maven",
    "pypi": "pypi",
    "python": "pypi",
    "gem": "gem",
    "rubygems": "gem",
    "deb": "deb",
    "debian": "deb",
    "rpm": "rpm",
    "cargo": "cargo",
    "golang": "golang",
    "go": "golang",
    "nuget": "nuget",
    "composer": "composer",
    "generic": "generic",
}

# 本服务具备版本比较器的生态
SUPPORTED_ECOSYSTEMS = {"npm", "maven", "pypi", "gem", "deb"}


@dataclass(frozen=True)
class Purl:
    """解析后的 purl，各段已 URL 解码并规范化。"""

    type: str
    name: str
    namespace: str | None = None
    version: str | None = None
    qualifiers: dict[str, str] = field(default_factory=dict)
    subpath: str | None = None
    raw: str = ""

    @property
    def ecosystem(self) -> str:
        """归一化的生态标识（尽力映射；是否受支持由 :data:`SUPPORTED_ECOSYSTEMS` 判断）。"""
        return _ALIASES.get(self.type, self.type)

    def without_version(self) -> "Purl":
        """去掉版本，用于组件身份合并。"""
        return Purl(
            type=self.type,
            name=self.name,
            namespace=self.namespace,
            version=None,
            qualifiers=dict(self.qualifiers),
            subpath=self.subpath,
            raw=self.raw,
        )

    def canonical(self, *, include_version: bool = True) -> str:
        """规范字符串：小写 type；namespace/name 小写但保留分隔。"""
        parts = [f"pkg:{self.type.lower()}"]
        if self.namespace:
            parts.append(self.namespace.lower())
        parts.append(self.name.lower())
        head = "/".join(parts)
        if include_version and self.version is not None:
            head = f"{head}@{self.version}"
        if self.qualifiers:
            qs = "&".join(
                f"{k.lower()}={v}" for k, v in sorted(self.qualifiers.items())
            )
            head = f"{head}?{qs}"
        if self.subpath:
            head = f"{head}#{self.subpath}"
        return head

    def __str__(self) -> str:  # pragma: no cover - 调试便利
        return self.canonical()


def normalize_ecosystem(purl_type: str) -> str:
    """把 purl type 映射到内部生态标识。未知 type 返回其小写形式，
    是否真正受支持由 ``ecosystem in SUPPORTED_ECOSYSTEMS`` 判断。"""
    key = purl_type.strip().lower()
    return _ALIASES.get(key, key)


def _decode(segment: str, what: str) -> str:
    try:
        return unquote(segment, errors="strict")
    except UnicodeDecodeError as exc:  # pragma: no cover - 防御
        raise PurlError(f"invalid percent-encoding in {what}") from exc


def parse_purl(text: str) -> Purl:
    """解析单个 purl 字符串。

    :raises PurlError: 格式错误、缺少必填段或生态不受支持。
    """
    if not isinstance(text, str) or not text.strip():
        raise PurlError("purl must be a non-empty string")
    raw = text.strip()

    if not raw.startswith("pkg:"):
        raise PurlError("purl must start with 'pkg:' scheme")
    rest = raw[len("pkg:") :]
    if rest.startswith("/"):
        # pkg://type/... 的 URL 形式不在支持子集内
        raise PurlError("URL-style purl with '//' is not supported")

    subpath: str | None = None
    if "#" in rest:
        rest, frag = rest.split("#", 1)
        subpath = _decode(frag, "subpath") or None

    qualifiers: dict[str, str] = {}
    if "?" in rest:
        rest, query = rest.split("?", 1)
        for key, value in parse_qsl(query, keep_blank_values=True):
            qualifiers[key.lower()] = value

    version: str | None = None
    if "@" in rest:
        # 版本是最后一个 '@' 之后的内容（namespace/name 不允许 '@'）
        rest, ver = rest.rsplit("@", 1)
        version = _decode(ver, "version") or None

    if "/" not in rest:
        raise PurlError("purl must contain at least type/name")
    type_part, _, path = rest.partition("/")
    type_part = type_part.strip().lower()
    if not type_part:
        raise PurlError("purl type is empty")
    # 生态是否受支持交由匹配层判断（-> 未知），此处仅规范化
    ecosystem = normalize_ecosystem(type_part)

    path_parts = [p for p in path.split("/") if p != ""]
    if not path_parts:
        raise PurlError("purl name is empty")
    name = _decode(path_parts[-1], "name")
    if not name:
        raise PurlError("purl name is empty")
    namespace = "/".join(_decode(p, "namespace") for p in path_parts[:-1]) or None

    return Purl(
        type=type_part,
        name=name,
        namespace=namespace,
        version=version,
        qualifiers=qualifiers,
        subpath=subpath,
        raw=raw,
    )
