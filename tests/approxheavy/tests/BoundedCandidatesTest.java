package approxheavy.tests;

import approxheavy.candidates.BoundedCandidates;

import java.util.HashMap;
import java.util.List;
import java.util.Map;

/** Bounded candidate set behavior: capacity, eviction by lowest estimate, no coverage claim. */
public final class BoundedCandidatesTest {
    public static void main(String[] args) {
        TestRunner runner = new TestRunner("BoundedCandidatesTest");

        runner.add("retains up to capacity without eviction", () -> {
            Map<String, Long> backing = new HashMap<>();
            BoundedCandidates c = new BoundedCandidates(3, k -> backing.getOrDefault(k, 0L));
            c.observe("a");
            c.observe("b");
            c.observe("c");
            TestRunner.checkEq(c.size(), 3, "size 3");
        });

        runner.add("evicts the lowest-estimate retained key when full", () -> {
            Map<String, Long> backing = new HashMap<>();
            backing.put("hot", 100L);
            backing.put("warm", 50L);
            backing.put("cold", 1L);
            backing.put("rising", 60L);
            BoundedCandidates c = new BoundedCandidates(3, k -> backing.getOrDefault(k, 0L));
            c.observe("hot");
            c.observe("warm");
            c.observe("cold");
            c.observe("rising");
            TestRunner.check(!c.contains("cold"), "cold must be evicted");
            TestRunner.check(c.contains("hot") && c.contains("warm") && c.contains("rising"),
                    "the three heaviest remain");
        });

        runner.add("duplicate observes do not grow the set", () -> {
            BoundedCandidates c = new BoundedCandidates(2, k -> 0L);
            c.observe("x");
            c.observe("x");
            c.observe("y");
            c.observe("x");
            TestRunner.checkEq(c.size(), 2, "still 2");
        });

        runner.add("topK is ordered by estimated count descending", () -> {
            Map<String, Long> backing = new HashMap<>();
            backing.put("a", 3L);
            backing.put("b", 9L);
            backing.put("c", 6L);
            BoundedCandidates c = new BoundedCandidates(5, k -> backing.getOrDefault(k, 0L));
            c.observe("a");
            c.observe("b");
            c.observe("c");
            List<Map.Entry<String, Long>> top = c.topK(2);
            TestRunner.checkEq(top.size(), 2, "k=2");
            TestRunner.check(top.get(0).getKey().equals("b"), "b first");
            TestRunner.check(top.get(1).getKey().equals("c"), "c second");
        });

        runner.add("topK(k) with k larger than set size returns everything", () -> {
            BoundedCandidates c = new BoundedCandidates(5, k -> 1L);
            c.observe("only");
            TestRunner.checkEq(c.topK(10).size(), 1, "one item");
        });

        runner.add("a displaced key demonstrates that coverage is not guaranteed", () -> {
            // A key whose true mass arrives late, after the set filled with
            // momentarily-heavier keys, may be absent forever: the structure
            // only re-considers a key when an occurrence is observed, and an
            // unlucky stream order can keep it out.
            Map<String, Long> backing = new HashMap<>();
            BoundedCandidates c = new BoundedCandidates(2, k -> backing.getOrDefault(k, 0L));
            backing.put("a", 100L);
            backing.put("b", 90L);
            backing.put("late", 80L);
            c.observe("a");
            c.observe("b");
            // At the moment 'late' first appears, estimates are still lower...
            backing.put("a", 100L);
            backing.put("b", 90L);
            backing.put("late", 1L);
            c.observe("late"); // evicted, estimate 1
            backing.put("late", 1_000L);
            // Without another observe('late'), the set has no idea it became hot.
            TestRunner.check(!c.contains("late"),
                    "late bloomer stays unknown -> no coverage guarantee");
        });

        runner.add("merge unions keys and respects capacity", () -> {
            Map<String, Long> backing = new HashMap<>();
            backing.put("a", 10L);
            backing.put("b", 20L);
            backing.put("x", 30L);
            BoundedCandidates c1 = new BoundedCandidates(2, k -> backing.getOrDefault(k, 0L));
            c1.observe("a");
            c1.observe("b");
            BoundedCandidates c2 = new BoundedCandidates(2, k -> backing.getOrDefault(k, 0L));
            c2.observe("x");
            c1.mergeWith(c2);
            TestRunner.checkEq(c1.size(), 2, "trimmed to capacity");
            TestRunner.check(c1.contains("x"), "heavier union key kept");
        });

        runner.run();
    }
}
