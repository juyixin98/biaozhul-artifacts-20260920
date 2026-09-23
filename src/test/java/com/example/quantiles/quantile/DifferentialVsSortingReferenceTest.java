package com.example.quantiles.quantile;

import org.junit.jupiter.api.RepeatedTest;
import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.List;
import java.util.Random;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * 验收核心：TreeMap 精确实现必须在任意插入/删除序列、任意 q 下与
 * “全排序参考实现”逐位一致。重复值按多重度占位。
 */
class DifferentialVsSortingReferenceTest {

    private static final double[] QS = {0.0, 0.1, 0.25, 0.33, 0.5, 0.75, 0.9, 0.99, 1.0};

    @Test
    void identicalAfterRandomInsertionsAndRemovals() {
        Random rnd = new Random(20260923L);
        for (int trial = 0; trial < 200; trial++) {
            var fast = new TreeMapAccumulator();
            var ref = new SortingReferenceAccumulator();
            List<Long> live = new ArrayList<>();

            int ops = 1 + rnd.nextInt(120);
            for (int op = 0; op < ops; op++) {
                boolean remove = !live.isEmpty() && rnd.nextInt(3) == 0;
                if (remove) {
                    Long victim = live.remove(rnd.nextInt(live.size()));
                    fast.remove(victim);
                    ref.remove(victim);
                } else {
                    // 值域刻意收窄制造大量重复；含负数
                    long v = rnd.nextInt(11) - 5;
                    live.add(v);
                    fast.add(v);
                    ref.add(v);
                }
                if (op % 7 == 0) {
                    assertAgree(fast, ref);
                }
            }
            assertAgree(fast, ref);
        }
    }

    @RepeatedTest(50)
    void randomSmallWindowsMatchReference() {
        Random rnd = new Random();
        var fast = new TreeMapAccumulator();
        var ref = new SortingReferenceAccumulator();
        int n = rnd.nextInt(30);
        for (int i = 0; i < n; i++) {
            long v = rnd.nextLong();
            fast.add(v);
            ref.add(v);
        }
        assertAgree(fast, ref);
    }

    @Test
    void allDuplicatesAgree() {
        var fast = new TreeMapAccumulator();
        var ref = new SortingReferenceAccumulator();
        for (int i = 0; i < 100; i++) {
            fast.add(4L);
            ref.add(4L);
        }
        for (int i = 0; i < 60; i++) {
            fast.remove(4L);
            ref.remove(4L);
        }
        assertAgree(fast, ref);
    }

    @Test
    void countsStayConsistentAfterRemovingAllDuplicates() {
        var fast = new TreeMapAccumulator();
        for (int i = 0; i < 5; i++) {
            fast.add(9L);
        }
        assertEquals(5, fast.count());
        for (int i = 0; i < 5; i++) {
            fast.remove(9L);
        }
        assertEquals(0, fast.count());
        // 全删后重新加入，验证内部计数已彻底归零
        fast.add(9L);
        assertEquals("9", fast.quantile(0.5).toCanonicalString());
        assertEquals(1, fast.count());
    }

    private static void assertAgree(TreeMapAccumulator fast,
                                    SortingReferenceAccumulator ref) {
        assertEquals(ref.count(), fast.count(), "count 不一致");
        if (fast.count() == 0) {
            return;
        }
        List<Double> qlist = new ArrayList<>();
        for (double q : QS) {
            qlist.add(q);
        }
        List<Fraction> a = fast.quantiles(qlist);
        List<Fraction> b = ref.quantiles(qlist);
        for (int i = 0; i < QS.length; i++) {
            assertEquals(b.get(i).toCanonicalString(), a.get(i).toCanonicalString(),
                    "q=" + QS[i] + " 时 TreeMap 实现与全排序参考不一致");
            assertTrue(a.get(i).denominator() >= 1);
        }
    }
}
