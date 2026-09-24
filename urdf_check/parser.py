"""安全的 XML 解析层。

防护目标：
* 禁止 XML 外部实体（XXE）——解析器禁用 DTD、实体加载、网络访问；
  同时在解析前拒绝任何 DOCTYPE / <!ENTITY ...> 声明（含内部实体、
  实体扩展炸弹），做到「根本不进入实体解析」。
* 不执行任意宏——含 xacro 属性或 <xacro:...> 元素的文件直接拒绝，
  要求用户在受信环境自行完成离线预展开后再提交。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from lxml import etree

from .issues import Code, Issue, Severity

# --- 预扫描：在任何 XML 解析之前处理，靠模式而非实体解析 ---

_COMMENT_RE = re.compile(rb"<!--.*?-->", re.DOTALL)
_CDATA_RE = re.compile(rb"<!\[CDATA\[.*?\]\]>", re.DOTALL)
_PI_RE = re.compile(rb"<\?.*?\?>", re.DOTALL)
_DOCTYPE_RE = re.compile(
    rb"<!DOCTYPE\b[^>\[]*(?:\[[^\]]*\]>[^<]*|[^>]*>)",
    re.IGNORECASE | re.DOTALL,
)
_ENTITY_DECL_RE = re.compile(rb"<!ENTITY\b", re.IGNORECASE)
# xacro 命名空间、xacro:xxx 元素、模板属性 ${...}
_XACRO_NS_RE = re.compile(
    rb"""xmlns\s*:\s*[A-Za-z0-9_.-]*\s*=\s*['"]http://www\.ros\.org/wiki/xacro""",
)
_XACRO_PREFIX_RE = re.compile(rb"<\s*xacro\s*:\s*\w+", re.IGNORECASE)
_XACRO_DOLLAR_RE = re.compile(rb"\$\{[^}]*\}")


def _strip_nonmarkup(data: bytes) -> bytes:
    """去掉注释 / CDATA / PI，避免这些区域里的文本被误判为标记。"""
    data = _COMMENT_RE.sub(b" ", data)
    data = _CDATA_RE.sub(b" ", data)
    data = _PI_RE.sub(b" ", data)
    return data


def _pre_scan(data: bytes) -> list[Issue]:
    issues: list[Issue] = []
    stripped = _strip_nonmarkup(data)

    if _DOCTYPE_RE.search(stripped):
        issues.append(Issue(
            Severity.ERROR, Code.DOCTYPE_FORBIDDEN,
            "禁止 <!DOCTYPE> 声明：URDF 不得包含 DTD（杜绝外部/内部实体）。",
        ))
    if _ENTITY_DECL_RE.search(stripped):
        issues.append(Issue(
            Severity.ERROR, Code.ENTITY_FORBIDDEN,
            "禁止 <!ENTITY ...> 实体声明；请提供不依赖实体的纯 XML。",
        ))
    if (_XACRO_NS_RE.search(stripped)
            or _XACRO_PREFIX_RE.search(stripped)
            or _XACRO_DOLLAR_RE.search(stripped)):
        issues.append(Issue(
            Severity.ERROR, Code.XACRO_FORBIDDEN,
            "检测到 xacro 宏/模板（xacro: 命名空间、<xacro:*> 或 ${...}）。"
            "本服务不执行任何宏，请先用 xacro 离线展开为纯 URDF。",
        ))
    return issues


def _hardened_parser() -> etree.XMLParser:
    return etree.XMLParser(
        attribute_defaults=False,
        dtd_validation=False,
        load_dtd=False,            # 不加载任何 DTD
        no_network=True,           # 禁止网络访问
        resolve_entities=False,    # 不解析实体
        huge_tree=False,           # 拒绝超大文档（缓解实体扩展炸弹）
        collect_ids=False,
    )


@dataclass
class ParsedDocument:
    root: etree._Element | None
    issues: list[Issue]


def parse_urdf(data: bytes | str) -> ParsedDocument:
    """解析 URDF；返回文档根节点与解析类问题（不包含语义检查）。"""
    if isinstance(data, str):
        data = data.encode("utf-8")

    issues = _pre_scan(data)
    if issues:
        # 预扫描命中即停止：带 DOCTYPE/实体/宏的文件根本不交给解析器。
        return ParsedDocument(root=None, issues=issues)

    parser = _hardened_parser()
    try:
        root = etree.fromstring(data, parser=parser)
    except etree.XMLSyntaxError as exc:
        line = exc.error_log[0].line if exc.error_log else 0
        issues.append(Issue(
            Severity.ERROR, Code.XML_PARSE_ERROR,
            f"XML 解析失败：{exc.msg}", line=line,
        ))
        return ParsedDocument(root=None, issues=issues)

    # 双保险：即使预扫描正则漏过，再向解析器询问一次 doctype。
    if root.getroottree().docinfo.doctype:
        issues.append(Issue(
            Severity.ERROR, Code.DOCTYPE_FORBIDDEN,
            "解析器报告文档包含 doctype，已拒绝。",
        ))
        return ParsedDocument(root=None, issues=issues)

    return ParsedDocument(root=root, issues=issues)
