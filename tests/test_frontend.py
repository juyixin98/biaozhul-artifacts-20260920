import unittest

from resflow.errors import LexError, ParseError, SemanticError

from tests._util import analyze


class LexerParserTests(unittest.TestCase):
    def test_lex_error_unknown_char(self):
        with self.assertRaises(LexError) as ctx:
            analyze("fun main() { let x = @; }")
        span = ctx.exception.span
        self.assertEqual(span.start.line, 1)
        self.assertEqual(span.start.column, 22)

    def test_lex_error_unterminated_string(self):
        with self.assertRaises(LexError):
            analyze('fun main() { throw "oops; }')

    def test_line_comment_and_locations(self):
        src = "// comment\nfun main() {\n    let f = acquire(\"x\");\n}\n"
        result = analyze(src)
        fn = result["functions"][0]
        acquire_node = next(n for n in fn["cfg"]["nodes"] if n["kind"] == "acquire")
        self.assertEqual(acquire_node["span"]["start"]["line"], 3)

    def test_parse_missing_semicolon(self):
        with self.assertRaises(ParseError):
            analyze("fun main() { let f = acquire(\"x\") }")

    def test_parse_empty_program(self):
        with self.assertRaises(ParseError):
            analyze("// only a comment")

    def test_else_if_desugars(self):
        src = """
        fun main(a) {
            if (a) {
                let f = acquire("x");
                release f;
            } else if (!a) {
                let g = acquire("y");
                release g;
            }
        }
        """
        result = analyze(src)
        self.assertEqual(result["counts"]["findings"], 0)


class SemanticTests(unittest.TestCase):
    def test_duplicate_function(self):
        with self.assertRaises(SemanticError):
            analyze("fun f() {} fun f() {}")

    def test_undeclared_function(self):
        with self.assertRaises(SemanticError):
            analyze("fun main() { missing(); }")

    def test_wrong_arity(self):
        with self.assertRaises(SemanticError):
            analyze("fun f(a) {} fun main() { f(); }")

    def test_unknown_variable(self):
        with self.assertRaises(SemanticError):
            analyze("fun main() { release nope; }")

    def test_uncaught_throw_requires_throws(self):
        with self.assertRaises(SemanticError):
            analyze("fun main() { throw \"x\"; }")

    def test_uncaught_throwing_call_requires_throws(self):
        with self.assertRaises(SemanticError):
            analyze(
                "fun f() throws { throw \"x\"; }\n"
                "fun main() { f(); }"
            )

    def test_throw_inside_try_is_caught(self):
        result = analyze(
            "fun main() {\n"
            "  let f = acquire(\"x\");\n"
            "  try { throw \"boom\"; } catch (e) { release f; }\n"
            "}"
        )
        self.assertEqual(result["counts"]["findings"], 0)

    def test_throw_in_catch_requires_outer_throws(self):
        with self.assertRaises(SemanticError):
            analyze(
                "fun main() {\n"
                "  try { } catch (e) { throw \"again\"; }\n"
                "}"
            )

    def test_calls_forbidden_in_conditions(self):
        with self.assertRaises(SemanticError):
            analyze("fun f() {} fun main() { if (f()) {} }")


if __name__ == "__main__":
    unittest.main()
