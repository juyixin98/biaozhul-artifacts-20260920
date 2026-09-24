"""解析安全：XXE / 内部实体 / 实体扩展炸弹 / xacro / 畸形 XML。"""

from urdf_check.issues import Code

from conftest import check, codes, has, single_link_urdf


VALID = single_link_urdf()


def test_valid_document_parses_clean():
    report = check(VALID)
    assert report.ok, report.to_dict()


def test_external_entity_reference_blocked():
    # 外部实体：DOCTYPE + ENTITY 必须在解析前被拒绝，且不得读取文件。
    xxe = """<?xml version="1.0"?>
<!DOCTYPE robot [
  <!ENTITY xxe SYSTEM "file:///etc/passwd">
]>
<robot name="evil">
  <link name="&xxe;"><inertial><mass value="1"/></inertial></link>
</robot>
"""
    report = check(xxe)
    assert not report.ok
    assert Code.DOCTYPE_FORBIDDEN in codes(report)
    assert Code.ENTITY_FORBIDDEN in codes(report)
    # 实体绝不能被展开成 /etc/passwd 内容
    assert all("root:" not in i.message for i in report.issues)


def test_external_entity_via_parameter_and_url_blocked():
    xxe = """<?xml version="1.0"?>
<!DOCTYPE robot [
  <!ENTITY % remote SYSTEM "http://127.0.0.1:9/evil.dtd">
  %remote;
]>
<robot name="x"><link name="a"/></robot>
"""
    report = check(xxe)
    assert has(report, Code.DOCTYPE_FORBIDDEN)
    assert has(report, Code.ENTITY_FORBIDDEN)


def test_internal_entity_billion_laughs_blocked_pre_parse():
    # 经典 billion laughs：即使是纯内部实体也直接拒绝，根本不进入扩展。
    xml = b"""<?xml version="1.0"?>
<!DOCTYPE lolz [
 <!ENTITY lol "lol">
 <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
 <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
]>
<robot name="&lol3;"><link name="a"/></robot>
"""
    report = check(xml)
    assert has(report, Code.DOCTYPE_FORBIDDEN)
    assert has(report, Code.ENTITY_FORBIDDEN)


def test_doctype_without_entity_still_forbidden():
    xml = """<?xml version="1.0"?>
<!DOCTYPE robot>
<robot name="x"><link name="a"/></robot>
"""
    assert has(check(xml), Code.DOCTYPE_FORBIDDEN)


def test_xacro_namespace_blocked():
    xml = """<?xml version="1.0"?>
<robot name="x" xmlns:xacro="http://www.ros.org/wiki/xacro">
  <xacro:macro name="m"/>
  <link name="a"/>
</robot>
"""
    report = check(xml)
    assert has(report, Code.XACRO_FORBIDDEN)


def test_xacro_element_and_dollar_template_blocked():
    xml = """<?xml version="1.0"?>
<robot name="x">
  <link name="a">
    <inertial><mass value="${2*3}"/></inertial>
  </link>
</robot>
"""
    assert has(check(xml), Code.XACRO_FORBIDDEN)


def test_dollar_expression_inside_comment_is_ignored():
    # 注释中的 ${...} 不应误判（先剥离注释再扫描）。
    xml = """<?xml version="1.0"?>
<robot name="x"><!-- ${not xacro} -->
  <link name="a"><inertial><mass value="1"/>
    <inertia ixx="1" ixy="0" ixz="0" iyy="1" iyz="0" izz="1"/></inertial></link>
</robot>
"""
    report = check(xml)
    assert not has(report, Code.XACRO_FORBIDDEN)


def test_malformed_xml_reports_parse_error():
    report = check("<robot name='x'><link name='a'></robot>")
    assert has(report, Code.XML_PARSE_ERROR)


def test_root_not_robot():
    xml = """<?xml version="1.0"?>
<foobar name="x"/>
"""
    report = check(xml)
    assert has(report, Code.ROOT_NOT_ROBOT)


def test_xxe_payload_file_is_rejected_end_to_end(tmp_path):
    p = tmp_path / "evil.urdf"
    p.write_text(
        '<?xml version="1.0"?>\n<!DOCTYPE r [<!ENTITY s SYSTEM '
        '"file:///etc/hostname">]>\n<robot name="&s;"/>',
        encoding="utf-8",
    )
    from urdf_check import inspect_urdf_file
    report = inspect_urdf_file(str(p))
    assert not report.ok
    assert has(report, Code.DOCTYPE_FORBIDDEN)
