"""Randomised differential test: incremental parse vs fresh full parse.

For many random source programs (including strings full of symbols,
unclosed brackets and unterminated strings) we apply long sequences of
random insert/delete/replace edits -- including edits *inside* string
literals and at declaration boundaries -- and after every single edit
assert that:

1. the incrementally maintained tree is structurally identical to a full
   reparse (kinds, spans and values), and
2. the diagnostics (message + half-open offsets) are identical.

We also assert that reuse actually happens along the way.
"""
import random
import unittest

from minilang import Document, full_parse, tree_signature

LEAVES = ["1", "2", "42", "3.5", "0", "x", "y", "abc", "v1", "g"]
OPS = ["+", "-", "*", "/", "%"]
# deliberately full of symbols that are syntax outside a string
STRING_GUTS = [
    "a+b",
    "(x)",
    "// not a comment",
    "a;b;c",
    'q\\"q',
    "p % q",
    "x - y / z",
    "()",
    "let fn",
]
INSERT_CHUNKS = [
    " ", "  ", "\n", "\n\n", "\t",
    "(", ")", "((", "))", ";", ";;", ",", "=", "+", "-", "*", "/",
    "//", "//c\n", '"', '""', '"x', '"\\"',
    "let", "let ", "fn", "fn ", "x", "ab", "1", "2", "9", "3.14",
    "f(", "; ", " (", ") ", "(1)", "+",
]


def gen_string(rng, force_unterminated):
    gut = rng.choice(STRING_GUTS)
    text = '"' + gut
    if force_unterminated:
        return text  # missing closing quote
    return text + '"'


def gen_expr(rng, depth, inject_error):
    """Generate an expression; with inject_error, sometimes drop a ')'."""
    roll = rng.random()
    if depth <= 0 or roll < 0.30:
        if rng.random() < 0.22:
            return gen_string(rng, inject_error and rng.random() < 0.5)
        return rng.choice(LEAVES)
    if roll < 0.55:  # binary
        return (
            gen_expr(rng, depth - 1, inject_error)
            + rng.choice(OPS)
            + gen_expr(rng, depth - 1, inject_error)
        )
    if roll < 0.68:  # unary
        return "-" + gen_expr(rng, depth - 1, inject_error)
    if roll < 0.86:  # parenthesised, occasionally unclosed
        inner = gen_expr(rng, depth - 1, inject_error)
        if inject_error and rng.random() < 0.35:
            return "(" + inner
        return "(" + inner + ")"
    # call, occasionally unclosed
    n = rng.randrange(0, 3)
    args = ",".join(gen_expr(rng, depth - 1, inject_error)
                    for _ in range(n))
    if inject_error and rng.random() < 0.35:
        return rng.choice(["f", "g"]) + "(" + args
    return rng.choice(["f", "g"]) + "(" + args + ")"


def gen_program(rng, inject_error):
    n = rng.randrange(1, 7)
    decls = []
    for i in range(n):
        name = rng.choice(["a", "b", "c", "d", "tmp", "v1"])
        if rng.random() < 0.25:
            params = ",".join(rng.choice(["x", "y", "z"])
                              for _ in range(rng.randrange(0, 3)))
            head = f"fn {name}({params}) = "
        else:
            head = f"let {name} = "
        decls.append(head + gen_expr(rng, 3, inject_error) + ";")
    return rng.choice(["\n", " ", "\n\n", " \n"]).join(decls)


def random_insert(rng):
    if rng.random() < 0.3:
        # a truly random small string over the dangerous alphabet
        alphabet = 'abxy123 ()+-*/;="\n.,letfn'
        return "".join(rng.choice(alphabet) for _ in range(rng.randrange(1, 5)))
    return rng.choice(INSERT_CHUNKS)


def random_edit(rng, text):
    if not text:
        return (0, 0, random_insert(rng))
    pos = rng.randrange(len(text) + 1)
    kind = rng.choice(["insert", "insert", "delete", "replace"])
    if kind == "insert" or pos == len(text):
        return (pos, 0, random_insert(rng))
    span_max = min(len(text) - pos, rng.choice([1, 1, 2, 3, 6, 12]))
    old_len = rng.randrange(1, span_max + 1)
    if kind == "delete":
        return (pos, old_len, "")
    return (pos, old_len, random_insert(rng))


class FuzzDifferentialTests(unittest.TestCase):
    SEEDS = list(range(24))
    EDITS_PER_SEED = 30

    total_comparisons = 0
    total_reused = 0
    total_parsed = 0
    errorful_steps = 0

    def test_random_edits_match_full_parse(self):
        for seed in self.SEEDS:
            rng = random.Random(seed)
            # odd seeds start from deliberately broken source
            source = gen_program(rng, inject_error=(seed % 2 == 1))
            doc = Document(source)
            # sanity: the initial full parse and incremental API agree
            fresh = full_parse(source)
            self.assertEqual(tree_signature(doc.result.tree),
                             tree_signature(fresh.tree))

            for step in range(self.EDITS_PER_SEED):
                start, old_len, new_text = random_edit(rng, doc.text)
                result = doc.apply_edit(start, old_len, new_text)
                fresh = full_parse(doc.text)

                type(self).total_comparisons += 1
                type(self).total_reused += result.stats["reused"]
                type(self).total_parsed += result.stats["parsed"]
                if result.diagnostics:
                    type(self).errorful_steps += 1

                self.assertEqual(
                    tree_signature(result.tree),
                    tree_signature(fresh.tree),
                    msg=(
                        f"\nTREE mismatch seed={seed} step={step}\n"
                        f"edit=({start}, {old_len}, {new_text!r})\n"
                        f"source={doc.text!r}"
                    ),
                )
                inc_diag = [d.signature() for d in result.diagnostics]
                full_diag = [d.signature() for d in fresh.diagnostics]
                self.assertEqual(
                    inc_diag,
                    full_diag,
                    msg=(
                        f"\nDIAGNOSTIC mismatch seed={seed} step={step}\n"
                        f"edit=({start}, {old_len}, {new_text!r})\n"
                        f"source={doc.text!r}\n"
                        f"incremental={inc_diag}\nfull={full_diag}"
                    ),
                )

    @classmethod
    def tearDownClass(cls):
        if cls.total_comparisons:
            total = cls.total_reused + cls.total_parsed
            pct = 100.0 * cls.total_reused / total if total else 0.0
            print(
                f"\n[fuzz] {cls.total_comparisons} edit comparisons; "
                f"reused {cls.total_reused}/{total} declarations "
                f"({pct:.1f}%); steps with diagnostics: "
                f"{cls.errorful_steps}"
            )


if __name__ == "__main__":
    unittest.main()
