package test.cms;

import java.util.List;
import java.util.Map;

import approx.cms.BoundedCandidateSet;
import approx.cms.CountMinSketch;
import test.Asserts;
import test.TestRunner;

public final class BoundedCandidateSetTest {

    private BoundedCandidateSetTest() {
    }

    public static void register(TestRunner r) {
        r.add("candidates: capacity is respected", BoundedCandidateSetTest::testCapacity);
        r.add("candidates: heavy items retained without collisions", BoundedCandidateSetTest::testHeavyRetained);
        r.add("candidates: topK ordered by descending estimate", BoundedCandidateSetTest::testTopKOrder);
        r.add("candidates: projected topK can miss true topK (no coverage guarantee)",
                BoundedCandidateSetTest::testNoCoverageGuarantee);
        r.add("candidates: k larger than set clips to size", BoundedCandidateSetTest::testClipsK);
    }

    private static void testCapacity() {
        CountMinSketch cms = new CountMinSketch(1024, 5, 0L);
        BoundedCandidateSet set = new BoundedCandidateSet(3, cms);
        for (int i = 0; i < 10; i++) {
            cms.add("x" + i);
            set.observe("x" + i);
        }
        Asserts.assertEquals(3, set.size(), "size capped at capacity");
    }

    private static void testHeavyRetained() {
        CountMinSketch cms = new CountMinSketch(2048, 5, 0L);
        BoundedCandidateSet set = new BoundedCandidateSet(5, cms);
        // Heavy items arrive first, lots of mass each.
        for (String h : new String[] {"h1", "h2", "h3", "h4", "h5"}) {
            for (int i = 0; i < 1000; i++) {
                cms.add(h);
                set.observe(h);
            }
        }
        // Then a flood of one-off items tries to evict them.
        for (int i = 0; i < 5000; i++) {
            String light = "lite-" + i;
            cms.add(light);
            set.observe(light);
        }
        Asserts.assertEquals(5, set.size(), "still 5 retained");
        for (String h : new String[] {"h1", "h2", "h3", "h4", "h5"}) {
            Asserts.assertTrue(set.contains(h), "heavy item retained: " + h);
        }
    }

    private static void testTopKOrder() {
        CountMinSketch cms = new CountMinSketch(2048, 5, 0L);
        BoundedCandidateSet set = new BoundedCandidateSet(5, cms);
        String[] items = {"a", "b", "c"};
        int[] weights = {10, 50, 30};
        for (int round = 0; round < 50; round++) {
            for (int i = 0; i < items.length; i++) {
                for (int j = 0; j < weights[i]; j++) {
                    cms.add(items[i]);
                    set.observe(items[i]);
                }
            }
        }
        List<Map.Entry<String, Long>> top = set.topK(3);
        Asserts.assertEquals("b", top.get(0).getKey(), "b heaviest");
        Asserts.assertEquals("c", top.get(1).getKey(), "c second");
        Asserts.assertEquals("a", top.get(2).getKey(), "a third");
    }

    private static void testNoCoverageGuarantee() {
        // With a tiny sketch and tiny capacity, late-arriving real heavy items can
        // miss the projection. This test asserts the *documented non-guarantee*:
        // we do NOT require the true top-K to be present.
        CountMinSketch cms = new CountMinSketch(4, 2, 0L);
        BoundedCandidateSet set = new BoundedCandidateSet(2, cms);
        // Fill the set early with two items, then a genuinely heavy newcomer arrives.
        for (String early : new String[] {"early1", "early2"}) {
            for (int i = 0; i < 30; i++) {
                cms.add(early);
                set.observe(early);
            }
        }
        for (int i = 0; i < 40; i++) {
            cms.add("late-heavy");
            set.observe("late-heavy");
        }
        List<Map.Entry<String, Long>> top = set.topK(2);
        // The projection has at most capacity entries regardless of true frequencies.
        Asserts.assertLessEqual(top.size(), 2L, "projection bounded by capacity");
    }

    private static void testClipsK() {
        CountMinSketch cms = new CountMinSketch(64, 3, 0L);
        BoundedCandidateSet set = new BoundedCandidateSet(5, cms);
        for (int i = 0; i < 3; i++) {
            cms.add("z" + i);
            set.observe("z" + i);
        }
        Asserts.assertEquals(3, set.topK(10).size(), "k clips to retained size");
        Asserts.assertEquals(0, set.topK(0).size(), "k=0 empty");
    }
}
