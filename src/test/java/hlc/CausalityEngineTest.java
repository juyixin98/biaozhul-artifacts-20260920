package hlc;

import java.util.List;
import java.util.Set;

import static hlc.CausalityEngine.Kind.LOCAL;
import static hlc.CausalityEngine.Kind.RECV;
import static hlc.CausalityEngine.Kind.SEND;

/**
 * Acceptance tests for the core contract:
 * happens-before(a,b)  &rArr;  ts(a) &lt; ts(b), and the converse is FALSE.
 */
public class CausalityEngineTest {

    @Test("causal chain across three skewed nodes produces strictly increasing timestamps")
    void causalChainWithSkew() {
        // bob's physical clock runs behind alice's; carol falls even further back at receive.
        CausalityEngine.Report r = CausalityEngine.run(List.of(
                new CausalityEngine.EventSpec("a1", "alice", SEND, 1_000_000, null),
                new CausalityEngine.EventSpec("c0", "carol", LOCAL, 1_000_500, null),
                new CausalityEngine.EventSpec("b1", "bob", RECV, 1_000_100, "a1"),
                new CausalityEngine.EventSpec("b2", "bob", SEND, 1_000_200, null),
                new CausalityEngine.EventSpec("c1", "carol", LOCAL, 1_000_600, null),
                new CausalityEngine.EventSpec("c2", "carol", RECV, 900_000, "b2")));

        TestRunner.assertTrue(r.soundnessHolds(),
                "every happens-before pair must be timestamp-ordered; violations=" + r.violations());

        HLCTimestamp a1 = ts(r, "a1");
        HLCTimestamp b1 = ts(r, "b1");
        HLCTimestamp b2 = ts(r, "b2");
        HLCTimestamp c2 = ts(r, "c2");
        TestRunner.assertLess(a1, b1, "a1 -> b1 (message)");
        TestRunner.assertLess(b1, b2, "b1 -> b2 (program order)");
        TestRunner.assertLess(b2, c2, "b2 -> c2 (message), even with carol's clock rolled back");
    }

    @Test("happens-before graph captures program order and transitive message links")
    void happensBeforeGraph() {
        CausalityEngine.Report r = CausalityEngine.run(List.of(
                new CausalityEngine.EventSpec("s", "a", SEND, 10, null),
                new CausalityEngine.EventSpec("m", "b", RECV, 20, "s"),
                new CausalityEngine.EventSpec("t", "b", SEND, 30, null),
                new CausalityEngine.EventSpec("u", "c", RECV, 40, "t")));

        Set<String> uPreds = r.happensBeforeGraph().get("u");
        TestRunner.assertTrue(uPreds.containsAll(List.of("t", "m", "s")),
                "u transitively happens-after the whole chain; got " + uPreds);
        TestRunner.assertTrue(r.soundnessHolds(), "no violations");
    }

    @Test("THE CONVERSE FAILS: timestamp order does not imply causality")
    void timestampOrderDoesNotImplyCausality() {
        // nodeX and nodeY never communicate. Every cross-node pair is concurrent.
        CausalityEngine.Report r = CausalityEngine.run(List.of(
                new CausalityEngine.EventSpec("x1", "nodeX", LOCAL, 2_000, null),
                new CausalityEngine.EventSpec("y1", "nodeY", LOCAL, 1_000, null),
                new CausalityEngine.EventSpec("x2", "nodeX", LOCAL, 3_000, null),
                new CausalityEngine.EventSpec("y2", "nodeY", LOCAL, 4_000, null)));

        TestRunner.assertTrue(r.soundnessHolds(), "within-node ordering remains sound");
        TestRunner.assertFalse(r.concurrentButOrdered().isEmpty(),
                "there must be timestamp-ordered pairs that are causally concurrent");

        // Concrete witness: y1 (l=1000) < x1 (l=2000) by timestamp, but they are concurrent,
        // because the nodes never exchanged a message and share no program order.
        boolean foundWitness = r.concurrentButOrdered().stream()
                .anyMatch(p -> p.earlier().equals("y1") && p.later().equals("x1")
                        && !p.happensBefore());
        TestRunner.assertTrue(foundWitness,
                "expected a y1<x1 timestamp order with no happens-before edge; got "
                        + r.concurrentButOrdered());

        // The graph itself must show no cross-node edges in either direction.
        TestRunner.assertFalse(r.happensBeforeGraph().get("x1").contains("y1"),
                "x1 must not happen-after y1");
        TestRunner.assertFalse(r.happensBeforeGraph().get("y1").contains("x1"),
                "y1 must not happen-after x1");
    }

    @Test("reordering the global interleaving keeps soundness but changes arbitrary order")
    void interleavingOrderMatters() {
        // Schedule y before x this time.
        CausalityEngine.Report yFirst = CausalityEngine.run(List.of(
                new CausalityEngine.EventSpec("y1", "nodeY", LOCAL, 1_000, null),
                new CausalityEngine.EventSpec("x1", "nodeX", LOCAL, 2_000, null)));
        CausalityEngine.Report xFirst = CausalityEngine.run(List.of(
                new CausalityEngine.EventSpec("x1", "nodeX", LOCAL, 2_000, null),
                new CausalityEngine.EventSpec("y1", "nodeY", LOCAL, 1_000, null)));
        TestRunner.assertTrue(yFirst.soundnessHolds() && xFirst.soundnessHolds(),
                "both interleavings remain causally sound");
        TestRunner.assertTrue(
                yFirst.concurrentButOrdered().size() == xFirst.concurrentButOrdered().size(),
                "number of concurrent ordered pairs is interleaving-independent (2)");
    }

    @Test("physical clock regression inside a scenario cannot break monotonicity")
    void regressionInScenario() {
        CausalityEngine.Report r = CausalityEngine.run(List.of(
                new CausalityEngine.EventSpec("e1", "a", LOCAL, 10_000, null),
                new CausalityEngine.EventSpec("e2", "a", LOCAL, 5_000, null),
                new CausalityEngine.EventSpec("e3", "a", LOCAL, 1_000, null)));
        TestRunner.assertTrue(r.soundnessHolds(), "monotonic despite regression");
        TestRunner.assertLess(ts(r, "e1"), ts(r, "e2"), "e1<e2");
        TestRunner.assertLess(ts(r, "e2"), ts(r, "e3"), "e2<e3");
    }

    @Test("invalid scenario input is rejected")
    void invalidInput() {
        TestRunner.assertThrows(HLCException.class, () -> CausalityEngine.run(List.of(
                new CausalityEngine.EventSpec("dup", "a", LOCAL, 1, null),
                new CausalityEngine.EventSpec("dup", "a", LOCAL, 2, null))));
        TestRunner.assertThrows(HLCException.class, () -> CausalityEngine.run(List.of(
                new CausalityEngine.EventSpec("r", "a", RECV, 1, "never-sent"))));
        TestRunner.assertThrows(HLCException.class, () ->
                new CausalityEngine.EventSpec("x", "a", RECV, 1, null));
    }

    private static HLCTimestamp ts(CausalityEngine.Report r, String label) {
        return r.results().stream()
                .filter(x -> x.label().equals(label))
                .findFirst().orElseThrow(() -> new AssertionError("missing event " + label))
                .timestamp();
    }
}
