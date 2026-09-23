"""全量解析测试：表达式优先级、声明结构、错误恢复与错误位置。"""

import unittest

from dl.parser import parse


def kinds(node):
    return [node.kind] + [k for c in node.children for k in kinds(c)]


def find(node, kind):
    if node.kind == kind:
        return node
    for c in node.children:
        r = find(c, kind)
        if r is not None:
            return r
    return None


class TestExpressions(unittest.TestCase):
    def test_precedence_times_binds_tighter(self):
        r = parse("x = 1 + 2 * 3;")
        top = find(r.tree, "Binary")
        self.assertEqual(top.text, "+")
        self.assertEqual(top.children[1].kind, "Binary")
        self.assertEqual(top.children[1].text, "*")

    def test_precedence_chain(self):
        r = parse("x = 1 || 2 && 3 == 4 < 5 + 6 * 7;")
        # 不断言整棵形状，只检查最低优先级在根
        top = find(r.tree, "Binary")
        self.assertEqual(top.text, "||")

    def test_parentheses_override(self):
        r = parse("x = (1 + 2) * 3;")
        top = find(r.tree, "Binary")
        self.assertEqual(top.text, "*")
        self.assertEqual(top.children[0].kind, "Group")
        self.assertEqual(top.children[0].children[0].text, "+")

    def test_unary(self):
        r = parse("x = -a + !b;")
        add = find(r.tree, "Binary")
        self.assertEqual(add.text, "+")
        self.assertEqual(add.children[0].kind, "Unary")
        self.assertEqual(add.children[0].text, "-")
        self.assertEqual(add.children[1].kind, "Unary")
        self.assertEqual(add.children[1].text, "!")

    def test_call_and_array(self):
        r = parse("x = f(a, g(b + c), [1, 2]);")
        self.assertIsNone(find(r.tree, "Junk"))
        call = find(r.tree, "Call")
        self.assertEqual(call.children[0].kind, "Ident")
        self.assertEqual(call.children[0].text, "f")
        self.assertEqual(call.children[1].kind, "ArgList")
        self.assertEqual(len(call.children[1].children), 3)
        arr = find(r.tree, "Array")
        self.assertEqual(len(arr.children), 2)

    def test_chained_calls(self):
        r = parse("x = f(1)(2);")
        calls = [n for n in _all(r.tree) if n.kind == "Call"]
        self.assertEqual(len(calls), 2)

    def test_string_symbol_does_not_break_parse(self):
        # 字符串里出现未配对 ) 与 ;，不得影响外层结构
        r = parse('var s = "a)b;c{d";\nvar t = 1;')
        self.assertEqual(r.errors, [])
        var_decls = [n for n in _all(r.tree) if n.kind == "VarDecl"]
        self.assertEqual(len(var_decls), 2)

    def test_literals(self):
        r = parse("x = true; y = false; z = null;")
        lits = [n for n in _all(r.tree) if n.kind == "Literal"]
        self.assertEqual([n.text for n in lits], ["true", "false", "null"])


class TestDeclarations(unittest.TestCase):
    def test_function_decl(self):
        src = "fn add(a, b) {\n  return a + b;\n}"
        r = parse(src)
        self.assertEqual(r.errors, [])
        fn = r.tree.children[0]
        self.assertEqual(fn.kind, "FnDecl")
        self.assertEqual(fn.children[0].text, "add")
        self.assertEqual(fn.children[1].kind, "ParamList")
        self.assertEqual([c.text for c in fn.children[1].children], ["a", "b"])
        self.assertEqual(fn.children[2].kind, "Block")
        self.assertEqual(fn.start, 0)
        self.assertEqual(fn.end, len(src))

    def test_if_else_chain(self):
        r = parse("if (a) { x; } else if (b) { y; } else { z; }")
        self.assertEqual(r.errors, [])
        iff = find(r.tree, "If")
        self.assertEqual(len(iff.children), 3)
        self.assertEqual(iff.children[2].kind, "Else")
        inner_if = iff.children[2].children[0]
        self.assertEqual(inner_if.kind, "If")
        self.assertEqual(inner_if.children[2].children[0].kind, "Block")

    def test_while_break_continue(self):
        src = "while (i < 10) { if (i == 3) { break; } continue; }"
        r = parse(src)
        self.assertEqual(r.errors, [])
        self.assertIsNotNone(find(r.tree, "While"))
        self.assertIsNotNone(find(r.tree, "Break"))
        self.assertIsNotNone(find(r.tree, "Continue"))

    def test_var_without_init(self):
        r = parse("var q;")
        self.assertEqual(r.errors, [])
        v = r.tree.children[0]
        self.assertEqual(v.kind, "VarDecl")
        self.assertEqual(len(v.children), 1)

    def test_empty_statement(self):
        r = parse(";;")
        self.assertEqual(r.errors, [])
        self.assertEqual([n.kind for n in r.tree.children], ["Empty", "Empty"])


class TestErrors(unittest.TestCase):
    def test_unterminated_paren_error_position(self):
        src = "x = (1 + 2;"
        r = parse(src)
        messages = [e.message for e in r.errors]
        self.assertTrue(any("未闭合的括号" in m for m in messages))
        err = next(e for e in r.errors if "未闭合的括号" in e.message)
        # 锚在分号令牌
        self.assertEqual((err.start, err.end), (src.index(";"), src.index(";") + 1))
        # 仍然解析出完整表达式结构
        self.assertIsNotNone(find(r.tree, "Group"))
        self.assertIsNotNone(find(r.tree, "Binary"))

    def test_unterminated_paren_at_eof(self):
        r = parse("x = (1 + 2")
        err = next(e for e in r.errors if "未闭合的括号" in e.message)
        self.assertEqual((err.start, err.end), (len("x = (1 + 2"),) * 2)

    def test_missing_rbrace_error_at_eof(self):
        src = "fn f() { x = 1;"
        r = parse(src)
        self.assertTrue(any("未闭合的语句块" in e.message for e in r.errors))

    def test_missing_semicolon_keeps_node(self):
        src = "var x = 1\nvar y = 2;"
        r = parse(src)
        self.assertTrue(any("缺少 ';'" in e.message for e in r.errors))
        # 两条声明都在
        self.assertEqual(len(r.tree.children), 2)
        err = next(e for e in r.errors if "缺少 ';'" in e.message)
        self.assertEqual((err.start, err.end), (src.index("var y"), src.index("var y") + 3))

    def test_missing_rhs(self):
        r = parse("x = 1 +;")
        messages = [e.message for e in r.errors]
        self.assertTrue(any("缺少表达式" in m for m in messages))
        self.assertTrue(any("右操作数" in m for m in messages))

    def test_structural_junk_recovery(self):
        src = "var = 3;\nvar ok = 1;"
        r = parse(src)
        self.assertEqual([n.kind for n in r.tree.children], ["Junk", "VarDecl"])
        junk = r.tree.children[0]
        self.assertEqual(junk.start, 0)  # 从 var 开始

    def test_illegal_char_reported(self):
        r = parse("x = @;")
        self.assertTrue(any("非法字符" in e.message for e in r.errors))

    def test_errors_sorted(self):
        r = parse("var = ;\nfn (\nx = (1;")
        starts = [e.start for e in r.errors]
        self.assertEqual(starts, sorted(starts))

    def test_empty_program(self):
        r = parse("")
        self.assertEqual(r.tree.kind, "Program")
        self.assertEqual(r.tree.children, [])
        self.assertEqual(r.errors, [])
        self.assertEqual((r.tree.start, r.tree.end), (0, 0))

    def test_leading_comment_program_span(self):
        r = parse("// hi\nvar x;")
        self.assertEqual(r.tree.start, 0)
        self.assertEqual(r.tree.end, len("// hi\nvar x;"))


def _all(node):
    yield node
    for c in node.children:
        yield from _all(c)


if __name__ == "__main__":
    unittest.main()
