import unittest

from resflow.errors import SemanticError

from tests._util import analyze, codes, function, path_kinds


CLEAN = """
fun fail() throws { throw "boom"; }
fun main() {
    let f = acquire("file");
    use(f);
    release f;
}
"""

PARTIAL_INIT = """
fun main(flag) {
    if (flag) {
        let conn = acquire("connection");
    }
    use(conn);
    release conn;
}
"""

DOUBLE_AND_UAF = """
fun fail() throws { throw "boom"; }
fun main(flag) throws {
    let h = acquire("mutex");
    if (flag) {
        release h;
        release h;
    } else {
        release h;
        use(h);
        fail();
    }
}
"""

LOOP_BALANCED = """
fun main(ready) {
    while (ready) {
        let buf = acquire("buffer");
        use(buf);
        release buf;
    }
}
"""

LOOP_LEAK = """
fun main(ready) {
    while (ready) {
        let buf = acquire("buffer");
        use(buf);
    }
}
"""

CATCH_CLEANUP = """
fun fail() throws { throw "io"; }
fun main() {
    let db = acquire("database");
    try {
        use(db);
        fail();
        release db;
    } catch (e) {
        release db;
    }
}
"""

EXC_EXIT_LEAK = """
fun risky() throws { throw "full"; }
fun main(flag) throws {
    let tmp = acquire("tempfile");
    if (flag) {
        risky();
        release tmp;
    } else {
        release tmp;
    }
}
"""

NESTED = """
fun boom() throws { throw "boom"; }
fun inner() throws {
    let a = acquire("socket");
    try {
        boom();
    } catch (e1) {
        release a;
        throw "wrapped";
    }
}
fun main() {
    let b = acquire("handle");
    try {
        inner();
    } catch (e2) {
        use(b);
        release b;
    }
}
"""


class AnalysisFindingTests(unittest.TestCase):
    def test_clean_program_has_no_findings(self):
        result = analyze(CLEAN)
        self.assertEqual(codes(result), set())
        main = function(result)
        self.assertEqual(len(main["paths"]), 1)
        self.assertEqual(main["paths"][0]["kind"], "normal")

    def test_partial_initialization_branch(self):
        result = analyze(PARTIAL_INIT)
        self.assertEqual(codes(result), {"USE_NOT_HELD", "RELEASE_NOT_HELD"})
        kinds = path_kinds(result)
        self.assertEqual(kinds.count("normal"), 2)
        # Findings only belong to the false branch path.
        paths = function(result)["paths"]
        flagged = [p for p in paths if p["finding_ids"]]
        self.assertEqual(len(flagged), 1)
        seq = " ".join(flagged[0]["node_sequence"])
        self.assertIn("false", str(flagged[0]["steps"]))

    def test_double_release_and_use_after_release(self):
        result = analyze(DOUBLE_AND_UAF)
        self.assertEqual(codes(result), {"DOUBLE_RELEASE", "USE_AFTER_RELEASE"})
        paths = function(result)["paths"]
        # No leak: exceptional path released h before fail().
        self.assertNotIn("RESOURCE_LEAK", codes(result))
        self.assertEqual({p["kind"] for p in paths}, {"normal", "exception"})

    def test_loop_balanced_bounded_unrolling(self):
        result = analyze(LOOP_BALANCED, loop_bound=2)
        self.assertEqual(codes(result), set())
        paths = function(result)["paths"]
        # 0 iterations, 1 iteration, 2 iterations, then bound cutoff (divergent)
        kinds = [p["kind"] for p in paths]
        self.assertEqual(kinds.count("normal"), 3)
        self.assertEqual(kinds.count("divergent"), 1)

    def test_loop_acquire_leak_and_overwrite(self):
        result = analyze(LOOP_LEAK, loop_bound=1)
        self.assertIn("RESOURCE_LEAK", codes(result))
        self.assertIn("OVERWRITE_HELD", codes(result))

    def test_exception_edge_caught_and_cleanup_ok(self):
        result = analyze(CATCH_CLEANUP)
        self.assertEqual(codes(result), set())
        main = function(result)
        kinds = [p["kind"] for p in main["paths"]]
        # fail() cannot return normally: the only completion is via the catch,
        # which releases db and then exits normally.
        self.assertEqual(kinds, ["normal"])
        catch_paths = [
            p for p in main["paths"]
            if any(s["kind"] == "catch" for s in p["steps"])
        ]
        self.assertEqual(len(catch_paths), 1)
        self.assertTrue(catch_paths[0]["finding_ids"] == [])
        # The dead release after fail() (line 8 in the test source) must not
        # appear on any path; the catch release (line 10) must.
        for p in main["paths"]:
            self.assertFalse(
                any(s["kind"] == "release" and s["span"]["start"]["line"] == 8 for s in p["steps"])
            )
        catch_path = catch_paths[0]
        self.assertTrue(
            any(s["kind"] == "release" and s["span"]["start"]["line"] == 10 for s in catch_path["steps"])
        )

    def test_exceptional_exit_leak(self):
        result = analyze(EXC_EXIT_LEAK)
        self.assertEqual(codes(result), {"RESOURCE_LEAK"})
        exc_paths = [p for p in function(result)["paths"] if p["kind"] == "exception"]
        self.assertEqual(len(exc_paths), 1)
        self.assertTrue(exc_paths[0]["finding_ids"])

    def test_nested_try_rethrow(self):
        result = analyze(NESTED)
        self.assertEqual(codes(result), set())
        # inner: normal path is impossible (boom never returns), exception path
        # leaves through 'wrapped' with a released.
        inner = function(result, "inner")
        self.assertEqual([p["kind"] for p in inner["paths"]], ["exception"])
        states = inner["paths"][0]["steps"][-1]["state_after"]
        self.assertEqual(states["a"], "RELEASED")


class SummariesTests(unittest.TestCase):
    def test_always_throwing_callee_has_no_spurious_normal_path(self):
        result = analyze(EXC_EXIT_LEAK)
        # The release following risky() is dead code and must never appear.
        main = function(result)
        for p in main["paths"]:
            seen_risky = False
            for step in p["steps"]:
                if seen_risky:
                    self.assertNotEqual(step["kind"], "release")
                if step["kind"] == "call":
                    seen_risky = True
        risky_summary = next(s for s in result["summaries"] if s["name"] == "risky")
        self.assertFalse(risky_summary["may_return"])
        self.assertTrue(risky_summary["may_throw"])

    def test_pure_function_summary(self):
        result = analyze("fun main() {}")
        s = next(x for x in result["summaries"] if x["name"] == "main")
        self.assertTrue(s["may_return"])
        self.assertFalse(s["may_throw"])

    def test_conditional_thrower_summary_both_outcomes(self):
        src = """
        fun main(flag) throws {
            if (flag) { throw "x"; }
        }
        """
        result = analyze(src)
        s = next(x for x in result["summaries"] if x["name"] == "main")
        self.assertTrue(s["may_return"])
        self.assertTrue(s["may_throw"])

    def test_infinite_recursion_is_divergent_without_findings(self):
        src = """
        fun spin() {
            spin();
        }
        fun main() {
            let f = acquire("x");
            spin();
            release f;
        }
        """
        result = analyze(src)
        self.assertEqual(codes(result), set())
        main = function(result)
        self.assertEqual([p["kind"] for p in main["paths"]], ["divergent"])


class ReproducibilityTests(unittest.TestCase):
    def test_results_byte_stable_across_runs(self):
        import json
        a = json.dumps(analyze(DOUBLE_AND_UAF), sort_keys=True)
        b = json.dumps(analyze(DOUBLE_AND_UAF), sort_keys=True)
        self.assertEqual(a, b)

    def test_path_steps_carry_state_and_spans(self):
        result = analyze(CLEAN)
        path = function(result)["paths"][0]
        for step in path["steps"]:
            self.assertIn("state_after", step)
            self.assertIn("span", step)
            self.assertTrue(step["span"]["start"]["line"] >= 1)
        acquire_step = next(s for s in path["steps"] if s["kind"] == "acquire")
        self.assertEqual(acquire_step["state_after"]["f"], "HELD")
        self.assertEqual(acquire_step["events"][0]["type"], "acquire")


class CfgShapeTests(unittest.TestCase):
    def test_exception_edges_present(self):
        result = analyze(
            "fun f() throws { throw \"x\"; }\n"
            "fun main() throws { f(); }"
        )
        main = function(result)
        exc_edges = [e for e in main["cfg"]["edges"] if e["kind"] == "exception"]
        self.assertTrue(exc_edges)
        self.assertTrue(all(e["dst"] == main["cfg"]["except_exit"] for e in exc_edges))

    def test_catch_routes_exception_edges(self):
        result = analyze(
            "fun main() {\n"
            "  let f = acquire(\"x\");\n"
            "  try { throw \"x\"; } catch (e) { release f; }\n"
            "}"
        )
        main = function(result)
        catch_ids = {n["id"] for n in main["cfg"]["nodes"] if n["kind"] == "catch"}
        exc_edges = [e for e in main["cfg"]["edges"] if e["kind"] == "exception"]
        self.assertTrue(exc_edges)
        for e in exc_edges:
            self.assertIn(e["dst"], catch_ids)


if __name__ == "__main__":
    unittest.main()
