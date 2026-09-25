"""Determinism and multi-function tests."""

import json
import unittest

from tests._util import analyze_text

PROGRAM = """fn one() {
  let c;
  acquire(a);
  if (c) { throw "x"; }
  release(a);
  return;
}

fn two() {
  acquire(b);
  let k = 0;
  while (k < 3) {
    use(b);
    let k = k + 1;
  }
  release(b);
  return;
}
"""


class TestReproducibility(unittest.TestCase):

    def test_repeated_runs_byte_identical(self):
        r1 = analyze_text(PROGRAM)
        r2 = analyze_text(PROGRAM)
        s1 = json.dumps(r1, sort_keys=True)
        s2 = json.dumps(r2, sort_keys=True)
        self.assertEqual(s1, s2)

    def test_node_trace_order_stable(self):
        r1 = analyze_text(PROGRAM)
        r2 = analyze_text(PROGRAM)
        for f1, f2 in zip(r1["functions"], r2["functions"]):
            t1 = [p["node_trace"] for p in f1["paths"]]
            t2 = [p["node_trace"] for p in f2["paths"]]
            self.assertEqual(t1, t2)

    def test_path_ids_are_contiguous(self):
        report = analyze_text(PROGRAM)
        for f in report["functions"]:
            ids = [p["id"] for p in f["paths"]]
            self.assertEqual(ids, list(range(len(ids))))

    def test_decision_records_reference_existing_nodes(self):
        report = analyze_text(PROGRAM)
        for f in report["functions"]:
            node_ids = {n["id"] for n in f["cfg"]["nodes"]}
            for p in f["paths"]:
                for d in p["decisions"]:
                    self.assertIn(d["node"], node_ids)

    def test_state_changes_form_valid_transitions(self):
        allowed = {
            ("unacquired", "acquire"): "held",
            ("held", "acquire"): "held",
            ("held", "release"): "released",
            ("released", "release"): "released",
            ("unacquired", "release"): "released",
            ("held", "use"): "held",
            ("released", "use"): "released",
            ("unacquired", "use"): "unacquired",
        }
        report = analyze_text(PROGRAM)
        for f in report["functions"]:
            for p in f["paths"]:
                for ch in p["state_changes"]:
                    self.assertEqual(
                        ch["after"],
                        allowed[(ch["before"], ch["op"])],
                        f"bad transition in {f['name']} path {p['id']}")

    def test_multiple_functions_analyzed_independently(self):
        report = analyze_text(PROGRAM)
        names = [f["name"] for f in report["functions"]]
        self.assertEqual(names, ["one", "two"])
        self.assertEqual(report["summary"]["function_count"], 2)


class TestStateEvolutionExplicit(unittest.TestCase):
    """The state-change log must reconstruct final state from scratch."""

    def test_replay_state_changes(self):
        report = analyze_text("""fn f(){
          acquire(x); use(x); release(x); return;
        }""")
        p = report["functions"][0]["paths"][0]
        state = {}
        for ch in p["state_changes"]:
            state[ch["resource"]] = ch["after"]
        self.assertEqual(state, p["final_states"])


if __name__ == "__main__":
    unittest.main()
