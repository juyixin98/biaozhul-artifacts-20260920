package test.cms;

import java.util.List;
import java.util.Map;

import approx.cms.ExactCounter;
import test.Asserts;
import test.TestRunner;

public final class ExactCounterTest {

    private ExactCounterTest() {
    }

    public static void register(TestRunner r) {
        r.add("exact: counts and topK ground truth", ExactCounterTest::testCountsAndTopK);
        r.add("exact: unseen item is zero", () -> {
            ExactCounter c = new ExactCounter();
            Asserts.assertEquals(0L, c.countOf("nope"), "zero unseen");
        });
        r.add("exact: bulk add", () -> {
            ExactCounter c = new ExactCounter();
            c.add("x", 100);
            Asserts.assertEquals(100L, c.countOf("x"), "bulk add");
            Asserts.assertEquals(100L, c.totalCount(), "total");
        });
        r.add("exact: topK tie broken lexicographically", ExactCounterTest::testTieBreak);
    }

    private static void testCountsAndTopK() {
        ExactCounter c = new ExactCounter();
        java.util.Random rnd = new java.util.Random(9);
        for (int i = 0; i < 5000; i++) {
            c.add("item-" + rnd.nextInt(20));
        }
        Asserts.assertEquals(5000L, c.totalCount(), "total");
        Asserts.assertEquals(20, c.distinctItems(), "distinct");
        List<Map.Entry<String, Long>> top = c.topK(3);
        Asserts.assertEquals(3, top.size(), "3 returned");
        Asserts.assertGreaterEqual(top.get(0).getValue(), top.get(1).getValue(), "descending");
        Asserts.assertGreaterEqual(top.get(1).getValue(), top.get(2).getValue(), "descending");
    }

    private static void testTieBreak() {
        ExactCounter c = new ExactCounter();
        c.add("charlie", 5);
        c.add("alpha", 5);
        c.add("bravo", 5);
        List<Map.Entry<String, Long>> top = c.topK(3);
        Asserts.assertEquals("alpha", top.get(0).getKey(), "lexical 1");
        Asserts.assertEquals("bravo", top.get(1).getKey(), "lexical 2");
        Asserts.assertEquals("charlie", top.get(2).getKey(), "lexical 3");
    }
}
