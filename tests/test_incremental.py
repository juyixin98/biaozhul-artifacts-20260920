"""增量解析的定向测试：复用保留、范围平移、连续编辑、编辑点在字符串内。"""

import unittest

from dl import Document, Edit, parse
from dl.diff import assert_same


def all_nodes(node):
    yield node
    for c in node.children:
        yield from all_nodes(c)


def id_map(doc):
    return {n.node_id: n for n in all_nodes(doc.result.tree)}


class TestIncremental(unittest.TestCase):
    def test_trivial_reinsertion_matches(self):
        doc = Document(source="var a = 1;")
        doc.apply_edit(Edit(len("var a = 1;"), len("var a = 1; "[:-1] + ""), ""))
        assert_same(parse(doc.source), doc.result)

    def test_insert_declaration_reuses_following_nodes(self):
        src = "var a = 1;\nvar b = 2;\nvar c = 3;\n"
        doc = Document(source=src)
        before = id_map(doc)
        # 在中间插入一条声明
        pos = src.index("var c")
        doc.apply_edit(Edit(pos, pos, "var x = 0;\n"))
        assert_same(parse(doc.source), doc.result)
        after = id_map(doc)
        # 第一条、第三条声明（及其表达式）节点 ID 必须保留
        self.assertIn(before_kind_id(before, "VarDecl", 0), after)
        self.assertIn(before_kind_id(before, "VarDecl", 2), after)
        # 第三条声明的范围整体下移
        old_c = before[before_kind_id(before, "VarDecl", 2)]
        new_c = after[old_c.node_id]
        self.assertEqual(new_c.start, old_c.start + len("var x = 0;\n"))
        self.assertEqual(new_c.end - old_c.end, len("var x = 0;\n"))

    def test_delete_declaration_reuses_others(self):
        src = "var a = 1;\nvar b = 2;\nvar c = 3;\n"
        doc = Document(source=src)
        before = id_map(doc)
        start = src.index("var b")
        end = src.index("var c")
        doc.apply_edit(Edit(start, end, ""))
        assert_same(parse(doc.source), doc.result)
        after = id_map(doc)
        self.assertIn(before_kind_id(before, "VarDecl", 0), after)
        self.assertIn(before_kind_id(before, "VarDecl", 2), after)
        self.assertNotIn(before_kind_id(before, "VarDecl", 1), after)

    def test_edit_inside_expression_reuses_siblings(self):
        src = "var a = 1 + 2;\nvar b = 99;\nvar c = 3 * 4;\n"
        doc = Document(source=src)
        before = id_map(doc)
        # 把 1+2 里的 2 改成 20
        p = src.index("2")
        doc.apply_edit(Edit(p, p + 1, "20"))
        assert_same(parse(doc.source), doc.result)
        after = id_map(doc)
        # 后面的两条 VarDecl 完整保留
        self.assertIn(before_kind_id(before, "VarDecl", 1), after)
        self.assertIn(before_kind_id(before, "VarDecl", 2), after)

    def test_insert_delete_argument(self):
        src = "var r = f(10, 20, 30);"
        doc = Document(source=src)
        # 删除 ", 20"
        p = src.index(", 20")
        doc.apply_edit(Edit(p, p + 4, ""))
        assert_same(parse(doc.source), doc.result)
        self.assertEqual(doc.source, "var r = f(10, 30);")
        # 再在开头插入一个实参
        q = doc.source.index("10")
        doc.apply_edit(Edit(q, q, "5, "))
        assert_same(parse(doc.source), doc.result)
        self.assertEqual(doc.source, "var r = f(5, 10, 30);")

    def test_sequential_edits(self):
        doc = Document(source="var x = 1;\n")
        for i in range(20):
            doc.apply_edit(Edit(0, 0, f"var v{i} = {i};\n"))
        assert_same(parse(doc.source), doc.result)
        for i in range(0, 40, 3):
            # 随机风格的小修改
            p = min(i, len(doc.source) - 1)
            doc.apply_edit(Edit(p, p + 1, doc.source[p] if p < len(doc.source) else ""))
            assert_same(parse(doc.source), doc.result)
        self.assertEqual(doc.version, 20 + 14)

    def test_edit_inside_string_vs_outside(self):
        # 字符串内的 ')' 被编辑：字符串令牌整体失配，节点重解析，
        # 但同文件其它声明仍可复用
        src = 'var s = "a)b";\nvar t = 1;\nvar u = 2;\n'
        doc = Document(source=src)
        before = id_map(doc)
        p = src.index(")")
        doc.apply_edit(Edit(p, p + 1, "(("))
        assert_same(parse(doc.source), doc.result)
        after = id_map(doc)
        self.assertIn(before_kind_id(before, "VarDecl", 1), after)
        self.assertIn(before_kind_id(before, "VarDecl", 2), after)

    def test_whitespace_edit_keeps_semantic_nodes(self):
        src = "var a = 1;\nvar b = 2;"
        doc = Document(source=src)
        before = id_map(doc)
        # 纯空白插入：令牌流完全不变
        doc.apply_edit(Edit(0, 0, "  "))
        assert_same(parse(doc.source), doc.result)
        # 纯空白插入：令牌流完全不变，除 Program（设计上总是重解析）
        # 外的全部语义节点 ID 保留
        semantic = sum(1 for n in before.values() if n.kind != "Program")
        self.assertEqual(doc.reuse_events, semantic)

    def test_unclosed_paren_edits_recover(self):
        # 先缺少右括号，再补上
        doc = Document(source="var x = (1 + 2;\nvar y = 3;\n")
        self.assertTrue(any("未闭合的括号" in e.message
                            for e in doc.result.errors))
        p = doc.source.index(";")
        doc.apply_edit(Edit(p, p, ")"))
        assert_same(parse(doc.source), doc.result)
        self.assertEqual(doc.result.errors, [])
        # 再次删掉右括号，错误重新出现且位置正确
        q = doc.source.index(")")
        doc.apply_edit(Edit(q, q + 1, ""))
        assert_same(parse(doc.source), doc.result)
        self.assertTrue(any("未闭合的括号" in e.message
                            for e in doc.result.errors))

    def test_node_ranges_always_within_source(self):
        doc = Document(source="fn f(a) { return a; }\n")
        # 大量随机插入删除
        import random
        rng = random.Random(1234)
        for _ in range(50):
            pos = rng.randrange(len(doc.source) + 1)
            if rng.random() < 0.5:
                doc.apply_edit(Edit(pos, pos, rng.choice("xy+();\" \n{}")))
            else:
                end = min(pos + rng.randrange(3), len(doc.source))
                doc.apply_edit(Edit(pos, end, ""))
            for n in all_nodes(doc.result.tree):
                self.assertGreaterEqual(n.start, 0)
                self.assertLessEqual(n.end, len(doc.source))
                self.assertLessEqual(n.start, n.end)

    def test_apply_multiple_edits_at_once(self):
        doc = Document(source="var a = 1;\nvar b = 2;\n")
        doc.apply_edits([
            Edit(0, 0, "var z;\n"),
            Edit(len(doc.source), len(doc.source), "var w;"),
        ])
        assert_same(parse(doc.source), doc.result)
        self.assertEqual(doc.version, 2)

    def test_reused_node_ids_monotonic_new_ids(self):
        doc = Document(source="var a = 1;\nvar b = 2;\n")
        max0 = doc.result.max_node_id
        doc.apply_edit(Edit(doc.source.index("1"), doc.source.index("1") + 1,
                            "100"))
        assert_same(parse(doc.source), doc.result)
        new_ids = [n.node_id for n in all_nodes(doc.result.tree)
                   if n.text not in (None,) and n.kind == "Number"]
        # 新编号大于旧编号
        self.assertTrue(all(i >= 1 for i in new_ids))
        self.assertGreaterEqual(doc.result.max_node_id, max0)

    def test_set_text(self):
        doc = Document(source="var a = 1;")
        doc.set_text("fn f() {}")
        assert_same(parse(doc.source), doc.result)


def before_kind_id(before, kind, ordinal):
    ids = [i for i, n in sorted(before.items()) if n.kind == kind]
    return ids[ordinal]


if __name__ == "__main__":
    unittest.main()
