"""Hand-authored acceptance cases with exact expected results.

Every entry pins concrete offsets and a concrete failure category -- never a
mere "endpoint works" assertion. Expected values were derived by hand (and
double-checked against tests/fixtures/oracle.py, which is independent of the
product kernel).
"""
from __future__ import annotations

# (label, text, exact expected analysis)
# analysis = {
#   "matches":     [(open, close, type), ...],
#   "mismatches":  [(open, close), ...],
#   "stray_open":  [offset, ...],
#   "stray_close": [offset, ...],
#   "balanced":    bool,
# }
HAND_CASES = [
    (
        "simple_pairs",
        "(a)[b]{c}",
        {
            "matches": [(0, 2, "paren"), (3, 5, "square"), (6, 8, "brace")],
            "mismatches": [],
            "stray_open": [],
            "stray_close": [],
            "balanced": True,
        },
    ),
    (
        # The key acceptance case: counts are equal (one of each type) but the
        # pairs CROSS. Net counts alone would wrongly call this balanced.
        "counts_equal_but_crossing",
        "([)]",
        {
            "matches": [],
            # ')' at 2 meets '[' at 1; ']' at 3 meets '(' at 0.
            "mismatches": [(1, 2), (0, 3)],
            "stray_open": [],
            "stray_close": [],
            "balanced": False,
        },
    ),
    (
        "nested_multi_type",
        "(([{( )}]))",
        {
            # positions: (0 (1 [2 {3 (4 sp5 )6 }7 ]8 )9 )10
            "matches": [(4, 6, "paren"), (3, 7, "brace"),
                        (2, 8, "square"), (1, 9, "paren"), (0, 10, "paren")],
            "mismatches": [],
            "stray_open": [],
            "stray_close": [],
            "balanced": True,
        },
    ),
    (
        "brackets_inside_string_are_content",
        '( ] "[" )',
        # 0( 1sp 2] -> mismatch (0,2); quote at 4..7 masks '[' at 5;
        # then ) at 8 with empty stack -> stray close.
        {
            "matches": [],
            "mismatches": [(0, 2)],
            "stray_open": [],
            "stray_close": [8],
            "balanced": False,
        },
    ),
    (
        "escaped_quote_keeps_span_open",
        '"a\\"b([)]c"',
        # 0 " ; 1 a ; 2 \\ ; 3 " (escaped) ; 4 b ; 5( 6[ 7) 8] 9c ; 10 " close
        # Everything structural-looking is masked -> balanced, no tokens.
        {
            "matches": [],
            "mismatches": [],
            "stray_open": [],
            "stray_close": [],
            "balanced": True,
        },
    ),
    (
        "escaped_backslash_then_real_quote",
        '"\\\\" ( )',
        # 0 " 1 \\ 2 \\ (escaped pair) 3 " closes ; 5( 7)
        {
            "matches": [(5, 7, "paren")],
            "mismatches": [],
            "stray_open": [],
            "stray_close": [],
            "balanced": True,
        },
    ),
    (
        "brackets_in_line_comment",
        "// ([)]\n()",
        {
            "matches": [(0 + 8, 0 + 9, "paren")],  # newline at 7, '('8 ')'9
            "mismatches": [],
            "stray_open": [],
            "stray_close": [],
            "balanced": True,
        },
    ),
    (
        "brackets_in_block_comment",
        "/* ([)] */ ( )",
        # comment closes after offset 9; '(' at 11, ')' at 13
        {
            "matches": [(11, 13, "paren")],
            "mismatches": [],
            "stray_open": [],
            "stray_close": [],
            "balanced": True,
        },
    ),
    (
        "stray_open_runs_to_eof",
        "ab(cd",
        {
            "matches": [],
            "mismatches": [],
            "stray_open": [2],
            "stray_close": [],
            "balanced": False,
        },
    ),
    (
        "stray_close_with_empty_stack",
        "ab)cd",
        {
            "matches": [],
            "mismatches": [],
            "stray_open": [],
            "stray_close": [2],
            "balanced": False,
        },
    ),
]

# (label, text, edit) -> expected shortest-defect after the edit:
# (category, [start, end])
SHORTEST_DEFECT_CASES = [
    ("crossing_is_shorter_than_its_partner", "([)]xx",
     None, ("TYPE_MISMATCH", [1, 3])),
    ("lone_stray_close_is_point", "(( )) )",
     None, ("STRAY_CLOSE", [6, 7])),
]

# Exact match-jump expectations: (text, queried offset -> partner offset)
MATCH_CASES = [
    ("(a[b]c)", {0: 6, 6: 0, 2: 4, 4: 2}),
    ("(([{( )}]))", {0: 10, 1: 9, 2: 8, 3: 7, 4: 6}),
]
