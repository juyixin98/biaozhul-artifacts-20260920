"""别名归一化与原文位置：不能丢失原始提及，位置是真实 Unicode 码点偏移。"""
from __future__ import annotations

import io

from app.rules_engine import compile_pack_json, extract


def _upload(client, headers, text, name="d.txt"):
    resp = client.post(
        "/api/documents",
        data={"file": (io.BytesIO(text.encode("utf-8")), name)},
        headers=headers,
        content_type="multipart/form-data",
    )
    assert resp.status_code == 201, resp.data
    return resp.get_json()


def test_alias_surface_forms_preserved(ws, client, drain):
    text = "李教授在会上发言，随后李工补充，最后李明作总结。"
    up = _upload(client, ws["headers"], text)
    drain()
    data = client.get(f"/api/documents/{up['doc_id']}/entities", headers=ws["headers"]).get_json()
    persons = [e for e in data["entities"] if e["entity_type"] == "person"]
    liming = next(e for e in persons if e["canonical"] == "李明")
    surfaces = sorted(m["matched_text"] for m in liming["mentions"])
    assert surfaces == ["李工", "李教授", "李明"], surfaces
    # 每条提及的 alias（词典登记形式）与 matched_text（原文形式）都保留
    for m in liming["mentions"]:
        assert m["char_start"] >= 0 and m["char_end"] > m["char_start"]
        assert text[m["char_start"]:m["char_end"]] == m["matched_text"]
        assert m["rule_id"]


def test_case_insensitive_alias_keeps_original_case(ws, client, drain):
    text = "alan kay 与 KAY 在 PARC 工作，Kay 后来获奖。"
    up = _upload(client, ws["headers"], text)
    drain()
    data = client.get(f"/api/documents/{up['doc_id']}/entities", headers=ws["headers"]).get_json()
    persons = [e for e in data["entities"] if e["entity_type"] == "person"]
    alan = next(e for e in persons if e["canonical"] == "Alan Kay")
    matched = [m["matched_text"] for m in alan["mentions"]]
    # 原文大小写完整保留
    assert "alan kay" in matched and "KAY" in matched and "Kay" in matched, matched
    # 位置按原文切出一致
    for m in alan["mentions"]:
        assert text[m["char_start"]:m["char_end"]] == m["matched_text"]


def test_unicode_offsets_are_codepoints(ws, client, drain):
    # 含多字节字符 + emoji，偏移必须是 Unicode 码点而非 UTF-8 字节
    text = "🎉 北京大学 与 北大 🀄\n李明说你好"
    up = _upload(client, ws["headers"], text)
    drain()
    data = client.get(f"/api/documents/{up['doc_id']}/entities", headers=ws["headers"]).get_json()
    orgs = [e for e in data["entities"] if e["entity_type"] == "org"]
    canon = {e["canonical"]: e for e in orgs}
    assert "北京大学" in canon
    pku = canon["北京大学"]
    m = pku["mentions"][0]
    assert text[m["char_start"]:m["char_end"]] == "北京大学"
    assert m["char_start"] == 2  # 🎉(1) + 空格(1)


def test_engine_directly_deterministic_and_explainable():
    pack = compile_pack_json(
        open("rules/builtin_v1.json", encoding="utf-8").read()
    )
    text = "2024-03-15 李明说，SQLite 发布了新版本。"
    ms1 = extract(text, pack)
    ms2 = extract(text, pack)
    assert [(m.start, m.end, m.rule_id, m.canonical) for m in ms1] == \
           [(m.start, m.end, m.rule_id, m.canonical) for m in ms2]
    # 每条命中都能解释：有规则 ID、有位置、有规范名
    for m in ms1:
        assert m.rule_id.startswith(("person.", "org.", "tech.", "date."))
        assert text[m.start:m.end] == m.matched_text
        assert m.canonical


def test_alias_grouping_multiple_mentions_one_entity(ws, client, drain):
    """同一规范实体的多处提及聚成一个实体、多行 mention，不生成重复实体。"""
    text = "华为技术有限公司今天发布产品，华为还宣布了合作。"
    up = _upload(client, ws["headers"], text)
    drain()
    data = client.get(f"/api/documents/{up['doc_id']}/entities", headers=ws["headers"]).get_json()
    huawei = [e for e in data["entities"]
              if e["entity_type"] == "org" and e["canonical"] == "华为技术有限公司"]
    assert len(huawei) == 1
    assert len(huawei[0]["mentions"]) == 2
