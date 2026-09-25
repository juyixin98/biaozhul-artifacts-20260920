"""Analyzer acceptance tests.

Naming convention used by the assertions:

* alarm:        a reported source -> sink finding (expected true positive)
* false alarm:  an alarm the design knowingly produces due to a stated
                approximation (conditional sanitization, k=0 context merge)
* safe sink:    a sink present in the program with no reaching taint
"""

import unittest

from taintlang.analyzer import Analyzer
from taintlang.builder import build
from taintlang.config import Config
from taintlang.parser import parse


def analyze(src, k=2, entry=("main",), tainted_entry_params=True,
            extra_config=None):
    data = {"k": k, "entry_points": list(entry)}
    if extra_config:
        data.update(extra_config)
    config = Config.from_dict(data)
    ir = build(parse(src), config, src)
    return Analyzer(ir, config,
                    tainted_entry_params=tainted_entry_params).analyze()


def sink_lines(result):
    """Map sink source line -> number of distinct source origins reaching it."""
    out = {}
    for f in result["findings"]:
        line = f["sink"]["span"]["start"]["line"]
        out[line] = out.get(line, 0) + 1
    return out


def safe_lines(result):
    return {f["span"]["start"]["line"] for f in result["sinks_without_taint"]}


class TestDirectTaint(unittest.TestCase):
    def test_direct_source_to_sink(self):
        r = analyze("func main() {\n  var x = source();\n  sink(x);\n}")
        self.assertEqual(sink_lines(r), {3: 1})

    def test_constant_is_clean(self):
        r = analyze('func main() {\n  sink("hi");\n}')
        self.assertEqual(r["findings"], [])
        self.assertEqual(safe_lines(r), {2})

    def test_sanitizer_blocks_taint(self):
        r = analyze(
            "func main() {\n"
            "  var x = source();\n"
            "  var y = sanitize(x);\n"
            "  sink(y);\n"
            "}")
        self.assertEqual(r["findings"], [])
        self.assertEqual(safe_lines(r), {4})

    def test_sanitizer_does_not_retroactively_clean_source(self):
        r = analyze(
            "func main() {\n"
            "  var x = source();\n"
            "  var y = sanitize(x);\n"
            "  sink(y);\n"
            "  sink(x);\n"
            "}")
        self.assertEqual(sink_lines(r), {5: 1})
        self.assertEqual(safe_lines(r), {4})

    def test_binop_propagates_either_operand(self):
        r = analyze(
            "func main() {\n"
            "  var a = source() + 1;\n"
            "  var b = 2 * a;\n"
            "  sink(b);\n"
            "}")
        self.assertEqual(sink_lines(r), {4: 1})


class TestCrossFunction(unittest.TestCase):
    def test_taint_through_return(self):
        src = (
            "func getData() {\n  return source();\n}\n"
            "func main() {\n  var x = getData();\n  sink(x);\n}")
        r = analyze(src)
        self.assertEqual(sink_lines(r), {6: 1})

    def test_taint_through_parameter(self):
        src = (
            "func log(v) {\n  sink(v);\n}\n"
            "func main() {\n  log(source());\n}")
        r = analyze(src)
        self.assertEqual(sink_lines(r), {2: 1})

    def test_taint_through_three_frames(self):
        src = (
            "func a(x) { return b(x); }\n"
            "func b(y) { return c(y); }\n"
            "func c(z) { return z; }\n"
            "func main() {\n  var v = a(source());\n  sink(v);\n}")
        r = analyze(src)
        self.assertEqual(sink_lines(r), {6: 1})
        # path must show parameter bindings and a call-return join
        steps = r["findings"][0]["paths"][0]["steps"]
        kinds = [s["kind"] for s in steps]
        self.assertIn("param", kinds)
        self.assertIn("call-return", kinds)

    def test_clean_call_argument_stays_clean(self):
        src = (
            "func id(v) { return v; }\n"
            "func main() {\n  var c = id(7);\n  sink(c);\n}")
        r = analyze(src)
        self.assertEqual(r["findings"], [])
        self.assertEqual(safe_lines(r), {4})

    def test_two_distinct_call_contexts(self):
        # same callee: tainted at one site, clean at another
        src = (
            "func id(v) { return v; }\n"
            "func main() {\n"
            "  var t = id(source());\n"
            "  sink(t);\n"
            "  var c = id(\"clean\");\n"
            "  sink(c);\n"
            "}")
        r = analyze(src, k=2)
        self.assertEqual(sink_lines(r), {4: 1})
        self.assertEqual(safe_lines(r), {6})
        self.assertGreaterEqual(r["stats"]["contexts"], 2)


class TestContextSensitivity(unittest.TestCase):
    def _prog(self):
        return (
            "func id(v) { return v; }\n"
            "func main() {\n"
            "  var t = id(source());\n"
            "  sink(t);\n"
            "  var c = id(\"clean\");\n"
            "  sink(c);\n"
            "}")

    def test_k2_keeps_contexts_apart(self):
        r = analyze(self._prog(), k=2)
        self.assertEqual(len(r["findings"]), 1)

    def test_k0_merges_contexts_known_false_alarm(self):
        # 0-CFA: both call sites share one callee context -> the clean
        # sink is also reported.  This is a DOCUMENTED false alarm.
        r = analyze(self._prog(), k=0)
        self.assertEqual(len(r["findings"]), 2)


class TestBranchingAndSanitization(unittest.TestCase):
    def test_unconditional_sanitization_on_both_branches(self):
        src = (
            "func main() {\n"
            "  var x = source();\n"
            "  if (x == 1) {\n"
            "    x = sanitize(x);\n"
            "  } else {\n"
            "    x = sanitize(x);\n"
            "  }\n"
            "  sink(x);\n"
            "}")
        r = analyze(src)
        # sanitized on both sides -> clean at merge
        self.assertEqual(r["findings"], [])
        self.assertEqual(safe_lines(r), {8})

    def test_conditional_sanitization_is_a_known_false_alarm(self):
        src = (
            "func main() {\n"
            "  var x = source();\n"
            "  if (x == 1) {\n"
            "    x = sanitize(x);\n"
            "  } else {\n"
            "    x = x;\n"
            "  }\n"
            "  sink(x);\n"
            "}")
        r = analyze(src)
        # the else branch leaves x tainted; branch conditions are not
        # interpreted -> exactly one (sound, may-be-spurious) alarm
        self.assertEqual(sink_lines(r), {8: 1})

    def test_branch_with_taint_on_only_one_side(self):
        src = (
            "func main() {\n"
            "  var x = \"clean\";\n"
            "  if (1 < 2) {\n"
            "    x = source();\n"
            "  }\n"
            "  sink(x);\n"
            "}")
        r = analyze(src)
        self.assertEqual(sink_lines(r), {6: 1})


class TestLoops(unittest.TestCase):
    def test_taint_introduced_in_loop_reaches_sink(self):
        src = (
            "func main() {\n"
            "  var x = \"clean\";\n"
            "  var i = 0;\n"
            "  while (i < 3) {\n"
            "    if (i == 1) { x = source(); }\n"
            "    i = i + 1;\n"
            "  }\n"
            "  sink(x);\n"
            "}")
        r = analyze(src)
        self.assertEqual(sink_lines(r), {8: 1})

    def test_sanitization_every_iteration_is_clean(self):
        # The pre-loop value is sanitized once (covers the zero-iteration
        # path, which the analyzer cannot distinguish since conditions are
        # not interpreted), and every subsequent iteration sanitizes too.
        src = (
            "func main() {\n"
            "  var x = sanitize(source());\n"
            "  var i = 0;\n"
            "  while (i < 3) {\n"
            "    x = sanitize(x);\n"
            "    i = i + 1;\n"
            "  }\n"
            "  sink(x);\n"
            "}")
        r = analyze(src)
        self.assertEqual(r["findings"], [])

    def test_zero_iteration_branch_is_known_false_alarm(self):
        # Taint is only sanitized inside the loop body.  Because loop
        # conditions are not interpreted, exiting with zero iterations is
        # considered possible -> the pre-loop taint reaches the merge.
        # DOCUMENTED false alarm, same approximation as conditional
        # sanitization.
        src = (
            "func main() {\n"
            "  var x = source();\n"
            "  var i = 0;\n"
            "  while (i < 3) {\n"
            "    x = sanitize(x);\n"
            "    i = i + 1;\n"
            "  }\n"
            "  sink(x);\n"
            "}")
        r = analyze(src)
        self.assertEqual(sink_lines(r), {8: 1})


class TestRecursion(unittest.TestCase):
    def test_direct_recursion_propagates_taint(self):
        src = (
            "func echo(n, v) {\n"
            "  if (n <= 0) { return v; }\n"
            "  return echo(n - 1, v);\n"
            "}\n"
            "func main() {\n"
            "  var y = echo(5, source());\n"
            "  sink(y);\n"
            "}")
        r = analyze(src, k=2)
        self.assertEqual(sink_lines(r), {7: 1})

    def test_recursion_terminates_under_unbounded_k(self):
        src = (
            "func f(n, v) {\n"
            "  if (n == 0) { return v; }\n"
            "  return f(n - 1, v);\n"
            "}\n"
            "func main() {\n"
            "  sink(f(100, source()));\n"
            "}")
        r = analyze(src, k=None, extra_config={"max_call_sites": 16})
        # bounded by max_call_sites; analysis must terminate and stay sound
        self.assertLessEqual(r["stats"]["contexts"], 17)
        self.assertEqual(sink_lines(r), {6: 1})

    def test_mutual_recursion_terminates(self):
        src = (
            "func isEven(n, v) {\n"
            "  if (n == 0) { return v; }\n"
            "  return isOdd(n - 1, v);\n"
            "}\n"
            "func isOdd(n, v) {\n"
            "  if (n == 0) { return v; }\n"
            "  return isEven(n - 1, v);\n"
            "}\n"
            "func main() {\n"
            "  sink(isEven(30, source()));\n"
            "}")
        r = analyze(src, k=2)
        self.assertEqual(sink_lines(r), {10: 1})


class TestOpaqueCalls(unittest.TestCase):
    def test_unknown_call_taints_result_if_argument_tainted(self):
        r = analyze(
            "func main() {\n"
            "  var x = external(source());\n"
            "  sink(x);\n"
            "}")
        self.assertEqual(sink_lines(r), {3: 1})
        self.assertEqual(r["opaque_calls"][0]["name"], "external")
        self.assertTrue(r["opaque_calls"][0]["result_assumed_tainted"])

    def test_unknown_call_with_clean_argument_is_clean(self):
        r = analyze(
            "func main() {\n"
            "  var x = external(123);\n"
            "  sink(x);\n"
            "}")
        self.assertEqual(r["findings"], [])


class TestEntryPoints(unittest.TestCase):
    def test_entry_parameters_are_taint_origins(self):
        # entry function parameter conservatively treated as attacker-controlled
        r = analyze("func main(req) {\n  sink(req);\n}",
                    tainted_entry_params=True)
        self.assertEqual(sink_lines(r), {2: 1})
        self.assertEqual(r["findings"][0]["source"]["kind"],
                         "entry-parameter")

    def test_entry_parameters_can_be_marked_clean(self):
        r = analyze("func main(req) {\n  sink(req);\n}",
                    tainted_entry_params=False)
        self.assertEqual(r["findings"], [])


class TestPathReconstruction(unittest.TestCase):
    def test_path_is_forward_ordered_source_to_sink(self):
        src = (
            "func g() { return source(); }\n"
            "func main() {\n  var x = g();\n  sink(x);\n}")
        r = analyze(src)
        path = r["findings"][0]["paths"][0]
        kinds = [s["kind"] for s in path["steps"]]
        self.assertEqual(kinds[0], "source")
        self.assertEqual(kinds[-1], "sink")
        # every step carries a line number when it has a span
        for step in path["steps"]:
            if "span" in step:
                self.assertGreaterEqual(step["span"]["start"]["line"], 1)

    def test_path_steps_carry_source_text(self):
        r = analyze("func main() {\n  var x = source();\n  sink(x);\n}")
        steps = r["findings"][0]["paths"][0]["steps"]
        texts = [s["span"]["text"] for s in steps if "span" in s]
        self.assertIn("source()", texts)
        self.assertIn("sink(x)", texts)


class TestSoundnessSmoke(unittest.TestCase):
    def test_no_sink_no_findings(self):
        r = analyze("func main() {\n  var x = source();\n  var y = x + 1;\n}")
        self.assertEqual(r["findings"], [])

    def test_multiple_origins_and_sinks(self):
        src = (
            "func main() {\n"
            "  var a = source();\n"
            "  var b = source();\n"
            "  sink(a);\n"
            "  sink(b);\n"
            "}")
        r = analyze(src)
        # two distinct origins at two distinct sinks -> 2 findings
        self.assertEqual(len(r["findings"]), 2)


if __name__ == "__main__":
    unittest.main()
