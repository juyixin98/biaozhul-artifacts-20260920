"""安全解析测试:外部实体、DTD、宏执行一律拒绝。"""

from urdf_check import check_urdf


def codes(report):
    return {d.code for d in report.diagnostics}


def test_doctype_rejected():
    payload = b"""<?xml version="1.0"?>
<!DOCTYPE robot [ <!ENTITY xxe SYSTEM "file:///etc/passwd"> ]>
<robot name="x"><link name="a"/></robot>
"""
    report = check_urdf(payload)
    assert not report.ok
    assert codes(report) & {"DTD_FORBIDDEN", "ENTITY_FORBIDDEN"}


def test_external_entity_not_resolved():
    payload = b"""<?xml version="1.0"?>
<!DOCTYPE robot [ <!ENTITY xxe SYSTEM "file:///etc/passwd"> ]>
<robot name="&xxe;"><link name="a"/></robot>
"""
    report = check_urdf(payload)
    assert not report.ok
    # 实体绝不展开:报告中不得出现 /etc/passwd 的内容
    for d in report.diagnostics:
        assert "root:" not in (d.message or "")


def test_internal_entity_expansion_rejected():
    # 实体扩展(billion laughs 形态)同样拒绝
    payload = b"""<?xml version="1.0"?>
<!DOCTYPE robot [
  <!ENTITY a "xxxxxxxxxx">
  <!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">
]>
<robot name="&b;"><link name="l"/></robot>
"""
    report = check_urdf(payload)
    assert not report.ok
    assert codes(report) & {"DTD_FORBIDDEN", "ENTITY_FORBIDDEN"}


def test_xacro_macro_rejected():
    payload = b"""<?xml version="1.0"?>
<robot name="x" xmlns:xacro="http://www.ros.org/wiki/xacro">
  <xacro:macro name="m" params="x"><link name="${x}"/></xacro:macro>
  <xacro:m x="a"/>
</robot>
"""
    report = check_urdf(payload)
    assert not report.ok
    assert "XACRO_FORBIDDEN" in codes(report)


def test_xacro_property_rejected():
    payload = b"""<?xml version="1.0"?>
<robot name="x" xmlns:xacro="http://www.ros.org/wiki/xacro">
  <xacro:property name="mass" value="1.0"/>
  <link name="a"/>
</robot>
"""
    report = check_urdf(payload)
    assert not report.ok
    assert "XACRO_FORBIDDEN" in codes(report)


def test_syntax_error_reported():
    report = check_urdf(b"<robot name='x'><link></robot>")
    assert not report.ok
    assert "XML_SYNTAX_ERROR" in codes(report)


def test_root_must_be_robot():
    report = check_urdf(b"<notrobot/>")
    assert not report.ok
    assert "XML_SYNTAX_ERROR" in codes(report)
