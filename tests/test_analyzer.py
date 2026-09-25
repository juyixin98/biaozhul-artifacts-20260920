"""Analyzer tests: the three acceptance scenarios plus defect detection."""

import unittest

from tests._util import first_function, codes, codes_by_path


def diag_map(fn):
    return {(d["code"], d["resource"]): d for d in fn["diagnostics"]}


class TestPartialInitialization(unittest.TestCase):
    """Acceptance scenario: branch partial initialization."""

    SRC = """fn partial() {
      let flag;
      acquire(a);
      if (flag) {
        acquire(r);
        release(r);
        release(a);
      }
      use(r);
      return;
    }"""

    def setUp(self):
        self.fn = first_function(self.SRC).to_dict()

    def test_two_paths_with_distinct_terminals(self):
        statuses = {(p["terminal"]) for p in self.fn["paths"]}
        self.assertEqual(statuses, {"exit"})
        self.assertEqual(self.fn["summary"]["path_count"], 2)

    def test_true_path_use_after_release(self):
        by_path = codes_by_path(self.fn)
        # path 0 follows the true (acquire) branch first because true
        # edges are ordered before false ones.
        self.assertIn("use_after_release", by_path[0])

    def test_false_path_use_unacquired_and_leak(self):
        by_path = codes_by_path(self.fn)
        self.assertIn("use_unacquired", by_path[1])
        self.assertIn("resource_leak", by_path[1])

    def test_final_states_differ_path_sensitively(self):
        finals = {p["id"]: p["final_states"] for p in self.fn["paths"]}
        self.assertEqual(finals[0], {"a": "released", "r": "released"})
        self.assertEqual(finals[1], {"a": "held", "r": "unacquired"})


class TestLoopAcquisition(unittest.TestCase):
    """Acceptance scenario: loop acquisition with bounded unrolling."""

    SRC = """fn loop_acquire() {
      let condition;
      while (condition) {
        acquire(conn);
        use(conn);
        release(conn);
      }
      release(conn);
      return;
    }"""

    def setUp(self):
        self.fn = first_function(self.SRC).to_dict()

    def test_unrolled_paths_plus_truncation(self):
        statuses = [p["status"] for p in self.fn["paths"]]
        self.assertEqual(statuses.count("loop_truncated"), 1)
        # false exits: at check #0, #1, #2 -> 3 completed paths
        self.assertEqual(statuses.count("completed"), 3)

    def test_truncation_path_records_state(self):
        trunc = next(p for p in self.fn["paths"]
                     if p["status"] == "loop_truncated")
        self.assertTrue(any(d.get("truncated") for d in trunc["decisions"]))
        # After two iterations conn was released; state retained.
        self.assertEqual(trunc["final_states"]["conn"], "released")

    def test_zero_iteration_release_unacquired(self):
        # Path that takes false at the very first check: conn never held.
        p0 = min(
            (p for p in self.fn["paths"] if p["status"] == "completed"),
            key=lambda p: len(p["node_trace"]))
        codes0 = [d["code"] for d in p0["diagnostics"]]
        self.assertIn("release_unacquired", codes0)
        self.assertEqual(p0["final_states"]["conn"], "released")

    def test_decision_iterations_recorded(self):
        # The truncation path records the two entered iterations and a
        # final marker for the iteration that could not be entered.
        trunc = next(p for p in self.fn["paths"]
                     if p["status"] == "loop_truncated")
        entered = [d["iteration"] for d in trunc["decisions"]
                   if d["edge"] == "true" and not d.get("truncated")]
        self.assertEqual(entered, [1, 2])
        markers = [d["iteration"] for d in trunc["decisions"]
                   if d.get("truncated")]
        self.assertEqual(markers, [3])

    def test_loop_bound_changes_path_count(self):
        fn = first_function(self.SRC, loop_bound=3).to_dict()
        # false exits at 0..3 = 4 completed + 1 truncated
        self.assertEqual(fn["summary"]["completed"], 4)
        self.assertEqual(fn["summary"]["loop_truncated"], 1)


class TestExceptionalExit(unittest.TestCase):
    """Acceptance scenario: exceptional edges participate in state."""

    SRC = """fn exceptional() {
      let flag;
      acquire(db);
      use(db);
      if (flag) {
        throw "backend refused";
      }
      release(db);
      return;
    }"""

    def setUp(self):
        self.fn = first_function(self.SRC).to_dict()

    def test_one_path_per_terminal(self):
        terms = sorted(p["terminal"] for p in self.fn["paths"])
        self.assertEqual(terms, ["exit", "uncaught"])

    def test_exception_path_leaks(self):
        exc = next(p for p in self.fn["paths"]
                   if p["terminal"] == "uncaught")
        codes_ = [d["code"] for d in exc["diagnostics"]]
        self.assertIn("resource_leak", codes_)
        self.assertEqual(exc["final_states"]["db"], "held")

    def test_normal_path_clean(self):
        norm = next(p for p in self.fn["paths"]
                    if p["terminal"] == "exit")
        codes_ = [d["code"] for d in norm["diagnostics"]]
        self.assertNotIn("resource_leak", codes_)
        self.assertEqual(norm["final_states"]["db"], "released")

    def test_trace_includes_exception_edge_destination(self):
        # node 2 is the function-level uncaught sink by construction.
        exc = next(p for p in self.fn["paths"]
                   if p["terminal"] == "uncaught")
        self.assertEqual(exc["node_trace"][-1], 2)


class TestTryCatchRecovery(unittest.TestCase):

    SRC = """fn recovery() {
      let thrown;
      let rethrow;
      acquire(a);
      try {
        if (thrown) {
          throw "first failure";
        }
      } catch (e) {
        if (rethrow) {
          throw "second failure";
        }
        release(a);
        return;
      }
      release(a);
      return;
    }"""

    def setUp(self):
        self.fn = first_function(self.SRC).to_dict()

    def test_three_paths(self):
        self.assertEqual(self.fn["summary"]["path_count"], 3)

    def test_rethrow_path_leaks_to_uncaught(self):
        exc = next(p for p in self.fn["paths"]
                   if p["terminal"] == "uncaught")
        self.assertEqual(exc["final_states"]["a"], "held")
        self.assertIn("resource_leak",
                      [d["code"] for d in exc["diagnostics"]])

    def test_caught_path_releases(self):
        # Caught but not rethrown: shortest exit-reaching path through
        # the catch block.
        caught = [p for p in self.fn["paths"]
                  if p["terminal"] == "exit"
                  and p["final_states"]["a"] == "released"]
        self.assertTrue(caught)

    def test_normal_fallthrough_path_releases(self):
        exits = [p for p in self.fn["paths"] if p["terminal"] == "exit"]
        # Every exit-reaching path (normal fallthrough and caught without
        # rethrow) must end with a released.
        for p in exits:
            self.assertEqual(p["final_states"]["a"], "released")


class TestDefectKinds(unittest.TestCase):

    def test_double_release(self):
        fn = first_function("""fn f(){
          acquire(h); release(h); let c;
          if (c) { release(h); }
          return;
        }""").to_dict()
        self.assertIn("double_release", codes(fn))

    def test_use_after_release_on_all_paths(self):
        fn = first_function("""fn f(){
          acquire(h); release(h); let c;
          if (c) { }
          use(h);
          return;
        }""").to_dict()
        by_path = codes_by_path(fn)
        for path_codes in by_path.values():
            self.assertIn("use_after_release", path_codes)

    def test_double_acquire_leaks_first_instance(self):
        fn = first_function("""fn f(){
          acquire(h); acquire(h); release(h); return;
        }""").to_dict()
        self.assertIn("double_acquire", codes(fn))

    def test_release_unacquired(self):
        fn = first_function("fn f(){ release(x); return; }").to_dict()
        self.assertIn("release_unacquired", codes(fn))

    def test_use_unacquired(self):
        fn = first_function("fn f(){ use(x); return; }").to_dict()
        self.assertIn("use_unacquired", codes(fn))

    def test_missing_release_on_normal_exit(self):
        fn = first_function("fn f(){ acquire(x); return; }").to_dict()
        self.assertIn("resource_leak", codes(fn))

    def test_clean_program_has_no_diagnostics(self):
        fn = first_function("""fn f(owned){
          let flag;
          acquire(local);
          if (flag) { use(owned); }
          release(local);
          release(owned);
          return;
        }""").to_dict()
        self.assertEqual(fn["diagnostics"], [])

    def test_owned_parameter_starts_held_and_must_release(self):
        fn = first_function("fn f(h){ use(h); return; }").to_dict()
        self.assertEqual(fn["initial_states"]["h"], "held")
        self.assertIn("resource_leak", codes(fn))

    def test_reacquire_after_release_is_legal(self):
        fn = first_function("""fn f(){
          acquire(h); release(h); acquire(h); release(h); return;
        }""").to_dict()
        self.assertEqual(fn["diagnostics"], [])

    def test_constant_true_branch_pruned(self):
        fn = first_function("""fn f(){
          acquire(h);
          if (true) { release(h); }
          if (false) { use(h); }
          return;
        }""").to_dict()
        self.assertEqual(fn["summary"]["path_count"], 1)
        self.assertEqual(fn["diagnostics"], [])


class TestInfiniteLoop(unittest.TestCase):

    def test_while_true_does_not_diverge(self):
        fn = first_function("""fn f(){
          while (true) { acquire(r); release(r); }
          return;
        }""").to_dict()
        self.assertEqual(fn["summary"]["completed"], 0)
        self.assertEqual(fn["summary"]["loop_truncated"], 1)

    def test_while_false_body_never_runs(self):
        fn = first_function("""fn f(){
          while (false) { acquire(r); }
          return;
        }""").to_dict()
        self.assertEqual(fn["summary"]["path_count"], 1)
        # r is never acquired and never touched; no diagnostics.
        self.assertEqual(fn["diagnostics"], [])


if __name__ == "__main__":
    unittest.main()
