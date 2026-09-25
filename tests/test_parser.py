import unittest

from minilang import full_parse
from minilang.nodes import Node


def flatten(node, path=""):
    """Flat list of (path, kind, start, end) for quick tree assertions."""
    rows = [(path or node.kind, node.kind, node.start, node.end)]
    for i, c in enumerate(node.children):
        rows.extend(flatten(c, f"{path or node.kind}.{i}"))
    return rows


class ParserBasicTests(unittest.TestCase):
    def test_let_declaration(self):
        r = full_parse("let x = 1;")
        self.assertEqual(r.diagnostics, [])
        prog = r.tree
        self.assertEqual(prog.kind, "program")
        self.assertEqual(len(prog.children), 1)
        decl = prog.children[0]
        self.assertEqual(decl.kind, "let")
        self.assertEqual(decl.value["name"], "x")
        self.assertEqual(decl.value["name_span"], {"start": 4, "end": 5})
        self.assertEqual((decl.start, decl.end), (0, 10))
        num = decl.children[0]
        self.assertEqual(num.kind, "number")
        self.assertEqual(num.value["text"], "1")
        self.assertEqual((num.start, num.end), (8, 9))

    def test_fn_declaration(self):
        r = full_parse("fn add(a, b) = a + b;")
        self.assertEqual(r.diagnostics, [])
        fn = r.tree.children[0]
        self.assertEqual(fn.kind, "fn")
        self.assertEqual(fn.value["name"], "add")
        self.assertEqual(
            [p["name"] for p in fn.value["params"]], ["a", "b"]
        )
        self.assertTrue(fn.value["closed_paren"])
        body = fn.children[0]
        self.assertEqual(body.kind, "binary")
        self.assertEqual(body.value["op"], "+")

    def test_precedence_and_associativity(self):
        r = full_parse("let x = 1 + 2 * 3;")
        binary = r.tree.children[0].children[0]
        # 1 + (2 * 3)
        self.assertEqual(binary.value["op"], "+")
        self.assertEqual(binary.children[0].kind, "number")
        self.assertEqual(binary.children[0].value["text"], "1")
        rhs = binary.children[1]
        self.assertEqual(rhs.kind, "binary")
        self.assertEqual(rhs.value["op"], "*")

        r = full_parse("let x = 10 - 2 - 3;")
        # (10 - 2) - 3, left associative
        top = r.tree.children[0].children[0]
        self.assertEqual(top.value["op"], "-")
        self.assertEqual(top.children[0].kind, "binary")
        self.assertEqual(top.children[0].value["op"], "-")
        self.assertEqual(top.children[0].children[0].value["text"], "10")
        self.assertEqual(top.children[1].value["text"], "3")

        r = full_parse("let x = 2 * -3 + 4;")
        top = r.tree.children[0].children[0]
        self.assertEqual(top.value["op"], "+")
        self.assertEqual(top.children[0].value["op"], "*")
        self.assertEqual(top.children[0].children[1].kind, "unary")

    def test_parenthesised_and_call(self):
        r = full_parse("let x = f(1, (2 + 3));")
        self.assertEqual(r.diagnostics, [])
        call = r.tree.children[0].children[0]
        self.assertEqual(call.kind, "call")
        self.assertEqual(call.children[0].kind, "ident")
        self.assertEqual(call.children[1].value["text"], "1")
        paren = call.children[2]
        self.assertEqual(paren.kind, "paren")
        self.assertTrue(paren.value["closed_paren"])

    def test_string_with_symbols(self):
        r = full_parse('let s = "a + (b); // c";')
        self.assertEqual(r.diagnostics, [])
        s = r.tree.children[0].children[0]
        self.assertEqual(s.kind, "string")
        self.assertEqual(s.value["text"], "a + (b); // c")
        self.assertTrue(s.value["terminated"])


class ParserErrorTests(unittest.TestCase):
    def test_unclosed_parenthesis_position(self):
        r = full_parse("let x = (1 + 2;")
        # Diagnostic must point at the offending '(' at offset 8.
        messages = [(d.message, d.start, d.end) for d in r.diagnostics]
        self.assertIn(("unclosed '('", 8, 9), messages)
        paren = r.tree.children[0].children[0]
        self.assertEqual(paren.kind, "paren")
        self.assertFalse(paren.value["closed_paren"])
        # the paren node ends at its inner expression, before the ';'
        self.assertEqual((paren.start, paren.end), (8, 14))

    def test_unclosed_call_parenthesis(self):
        r = full_parse("let x = f(1;")
        messages = [(d.message, d.start) for d in r.diagnostics]
        self.assertIn(("unclosed '('", 9), messages)

    def test_unterminated_string_node(self):
        r = full_parse('let s = "oops;')
        kinds = [d.message for d in r.diagnostics]
        self.assertIn("unterminated string literal", kinds)
        string = r.tree.children[0].children[0]
        self.assertEqual(string.kind, "string")
        self.assertFalse(string.value["terminated"])

    def test_missing_semicolon_recovery(self):
        r = full_parse("let x = 1\nlet y = 2;")
        messages = [d.message for d in r.diagnostics]
        self.assertIn("expected ';'", messages)
        self.assertEqual(len(r.tree.children), 2)
        self.assertEqual(r.tree.children[1].kind, "let")

    def test_junk_before_declaration(self):
        r = full_parse("123 456\nlet x = 1;")
        messages = [d.message for d in r.diagnostics]
        self.assertTrue(any("expected a declaration" in m for m in messages))
        kinds = [c.kind for c in r.tree.children]
        self.assertEqual(kinds, ["error", "let"])

    def test_missing_expression(self):
        r = full_parse("let x = ;")
        messages = [d.message for d in r.diagnostics]
        self.assertIn("expected expression", messages)
        # the semicolon is still consumed, no trailing "expected ';'"
        self.assertEqual(messages.count("expected ';'"), 0)

    def test_illegal_character_diagnostic(self):
        r = full_parse("let x = #;")
        messages = [d.message for d in r.diagnostics]
        self.assertTrue(any("illegal character" in m for m in messages))

    def test_multiple_errors_all_reported(self):
        r = full_parse("let x = (1; let s = \"a;")
        messages = [d.message for d in r.diagnostics]
        self.assertEqual(messages.count("unclosed '('"), 1)
        self.assertEqual(messages.count("unterminated string literal"), 1)


class PositionTests(unittest.TestCase):
    def test_all_descendants_inside_parent(self):
        text = "fn f(a) = (a + 1) * g(a);\nlet z = \"x\";\n"
        r = full_parse(text)

        def walk(node):
            for c in node.children:
                self.assertGreaterEqual(c.start, node.start)
                self.assertLessEqual(c.end, node.end)
                walk(c)

        walk(r.tree)

    def test_program_span_covers_whole_input(self):
        text = "  let x = 1;\n"
        r = full_parse(text)
        self.assertEqual((r.tree.start, r.tree.end), (0, len(text)))


if __name__ == "__main__":
    unittest.main()
