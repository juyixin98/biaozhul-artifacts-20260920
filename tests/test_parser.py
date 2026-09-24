import pytest

from app.language import LexError, ParseError, parse


def test_parse_minimal_program():
    prog = parse("func main() { var x = 1; }")
    assert prog.entry == "main"
    assert list(prog.functions) == ["main"]


def test_parse_full_constructs():
    src = """
    func add(a, b) {
        if (a == b and not false) {
            return a + b;
        } else {
            while (a < b) {
                a = a + 1;
            }
        }
        return nil;
    }
    func main() {
        var x = add(1, 2);
        x = x - 1;
    }
    """
    prog = parse(src)
    assert "add" in prog.functions
    assert prog.functions["add"].params == ["a", "b"]


def test_parse_error_on_missing_semicolon():
    with pytest.raises(ParseError):
        parse("func main() { var x = 1 }")


def test_lex_error_on_bad_char():
    with pytest.raises(LexError):
        parse("func main() { var x = @; }")


def test_missing_entry():
    with pytest.raises(ParseError):
        parse("func other() { }", entry="main")


def test_comments():
    src = """
    // line comment
    func main() { /* block
       comment */ var x = 1; // trailing
    }
    """
    prog = parse(src)
    assert prog.entry == "main"
