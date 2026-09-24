"""Shared pytest fixtures: small language programs used across test modules."""

import pytest

# --- cross-function propagation --------------------------------------------

CROSS_FN_DANGEROUS = """
func load() {
    var data = input();
    return data;
}
func render(x) {
    var y = concat("<", x, ">");
    sink(y);
}
func main() {
    var d = load();
    render(d);
}
"""

CROSS_FN_SAFE = """
func load() {
    var data = input();
    var safe = escape(data);
    return safe;
}
func render(x) {
    sink(x);
}
func main() {
    var d = load();
    render(d);
}
"""

# context sensitivity: same callee called with a tainted and a clean value
CONTEXT_SENSITIVE = """
func passthrough(x) {
    return x;
}
func main() {
    var bad = passthrough(input());
    var good = passthrough("literal");
    sink(good);
}
"""

# --- conditional sanitization ----------------------------------------------

CONDITION_SANITIZED = """
func main() {
    var x = input();
    if (validate(x)) {
        sink(x);
    }
}
"""

CONDITION_UNSANITIZED_BRANCH = """
func main() {
    var x = input();
    if (validate(x)) {
        sink(x);
    } else {
        sink(x);
    }
}
"""

CONDITION_NEGATED = """
func main() {
    var x = input();
    if (!validate(x)) {
        sink(x);
    }
}
"""

CONDITION_SANITIZER_VALUE = """
func main() {
    var x = input();
    var y = escape(x);
    sink(y);
}
"""

# equality to a literal must NOT be treated as sanitization (unsound shortcut)
CONDITION_LITERAL_EQUALITY_NOT_SANITIZER = """
func main() {
    var x = input();
    if (x == "admin") {
        sink(x);
    }
}
"""

# --- recursion --------------------------------------------------------------

RECURSION_DANGEROUS = """
func walk(n) {
    if (n == 0) {
        return input();
    }
    return walk(n - 1);
}
func main() {
    var x = walk(3);
    sink(x);
}
"""

RECURSION_SAFE = """
func walk(n) {
    if (n == 0) {
        return "base";
    }
    return walk(n - 1);
}
func main() {
    var x = walk(3);
    sink(x);
}
"""

RECURSION_MIXED = """
func walk(n) {
    if (n == 0) {
        return "base";
    }
    if (n == 5) {
        return input();
    }
    return walk(n - 1);
}
func main() {
    sink(walk(3));
}
"""

MUTUAL_RECURSION_DANGEROUS = """
func isEven(n) {
    if (n == 0) { return input(); }
    return isOdd(n - 1);
}
func isOdd(n) {
    if (n == 0) { return 0; }
    return isEven(n - 1);
}
func main() {
    sink(isEven(2));
}
"""

# --- loops ------------------------------------------------------------------

LOOP_TAINTED = """
func main() {
    var x = "clean";
    var i = 0;
    while (i < 3) {
        x = input();
        i = i + 1;
    }
    sink(x);
}
"""

LOOP_SANITIZED_IN_BODY = """
func main() {
    var x = input();
    var i = 0;
    while (i < 3) {
        if (validate(x)) {
            x = escape(x);
        }
        i = i + 1;
    }
    sink(x);
}
"""

LOOP_NEVER_RUNS = """
func main() {
    var x = "clean";
    while (false) {
        x = input();
    }
    sink(x);
}
"""

# source reaches sink through arithmetic and concatenation
ARITHMETIC_FLOW = """
func main() {
    var x = input();
    var y = x + 1;
    var z = concat("n=", y);
    sink(z);
}
"""

# undefined function call -> conservative taint warning
UNKNOWN_FUNCTION = """
func main() {
    var x = mystery(1);
    sink(x);
}
"""

# unreachable function must not be analyzed into a finding
DEAD_FUNCTION = """
func unused() {
    sink(input());
}
func main() {
    sink("clean");
}
"""

SINK_IN_CONDITION = """
func main() {
    var x = input();
    if (len(x) == 0) {
        sink(x);
    }
}
"""


@pytest.fixture
def programs():
    return {k: v for k, v in globals().items() if k.isupper() and isinstance(v, str)}
