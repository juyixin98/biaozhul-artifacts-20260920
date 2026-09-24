"""安全 XML 解析:禁止外部实体、DTD 与宏执行。

安全策略:
  * 禁用 DTD 加载、实体解析与网络访问(lxml 解析器层面);
  * 解析后再次拒绝 DOCTYPE 与任何实体节点(双保险,防 XXE/实体扩展);
  * 不执行 xacro 宏:检测到 xacro 命名空间内容即报错,绝不展开。
"""

from __future__ import annotations

from lxml import etree

XACRO_NAMESPACE = "http://www.ros.org/wiki/xacro"


class UnsafeXMLError(Exception):
    """输入 XML 违反安全策略(含 DTD/实体/宏)时抛出。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class URDFSyntaxError(Exception):
    """XML 语法错误(非安全问题)时抛出。"""

    def __init__(self, message: str, line: int | None = None):
        super().__init__(message)
        self.message = message
        self.line = line


def _build_parser() -> etree.XMLParser:
    return etree.XMLParser(
        resolve_entities=False,   # 不解析任何实体引用
        no_network=True,          # 禁止网络访问
        load_dtd=False,           # 不加载 DTD
        dtd_validation=False,     # 不做 DTD 校验
        attribute_defaults=False,  # 不从 DTD 注入默认属性
        recover=False,            # 语法错误即失败,不尝试恢复
        huge_tree=False,          # 限制超大文档,防资源耗尽
        strip_cdata=False,
    )


def parse_urdf(source: bytes) -> etree._Element:
    """安全解析 URDF 字节串,返回根元素。

     Raises:
        UnsafeXMLError: 含 DOCTYPE、实体节点或 xacro 宏内容。
        URDFSyntaxError: XML 语法非法或根元素不是 <robot>。
    """
    if isinstance(source, str):
        source = source.encode("utf-8")
    parser = _build_parser()
    try:
        root = etree.fromstring(source, parser=parser)
    except etree.XMLSyntaxError as exc:
        line = getattr(exc, "position", (None, None))[0]
        raise URDFSyntaxError(f"XML 语法错误: {exc}", line=line) from exc

    # 双保险:拒绝 DOCTYPE(解析器已不加载,这里直接判违规)
    doctype = root.getroottree().docinfo.doctype
    if doctype and doctype.strip():
        raise UnsafeXMLError(
            "DTD_FORBIDDEN",
            "禁止包含 DOCTYPE/DTD 声明(外部实体与实体扩展不予解析)",
        )

    # 拒绝任何实体节点(resolve_entities=False 时实体引用会以 Entity 节点残留)
    for entity in root.iter(etree.Entity):
        raise UnsafeXMLError(
            "ENTITY_FORBIDDEN",
            f"禁止实体引用 '&{entity.name};'(外部实体与实体扩展不予解析)",
        )

    # 拒绝 xacro 宏:本服务离线检查纯 URDF,绝不展开/执行宏
    xacro_used = False
    for el in root.iter():
        qname = etree.QName(el)
        if qname.namespace == XACRO_NAMESPACE:
            xacro_used = True
            break
        for attr in el.attrib:
            if etree.QName(attr).namespace == XACRO_NAMESPACE:
                xacro_used = True
                break
        if xacro_used:
            break
    if xacro_used:
        raise UnsafeXMLError(
            "XACRO_FORBIDDEN",
            "检测到 xacro 宏内容;本服务不执行任何宏,请先离线展开为纯 URDF 再提交",
        )

    if etree.QName(root).localname != "robot" or etree.QName(root).namespace:
        raise URDFSyntaxError("根元素必须是 <robot>(无命名空间)")

    return root
