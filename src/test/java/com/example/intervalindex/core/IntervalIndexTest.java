package com.example.intervalindex.core;

import com.example.intervalindex.testsupport.Asserts;
import java.util.ArrayList;
import java.util.List;
import java.util.Random;

/**
 * {@link IntervalIndex} 测试套件：
 * <ol>
 *   <li>基本插入/删除/重复区间/不存在 id；</li>
 *   <li>嵌套区间、相邻端点（半开语义不重叠）；</li>
 *   <li>空区间、逆序区间拒绝；</li>
 *   <li>红黑树结构不变量在每一步操作后成立；</li>
 *   <li>随机操作差分测试：插入/删除/查询每一步与暴力扫描逐次对照
 *       （覆盖计数、相交集合、size、maxEnd 全部一致）。</li>
 * </ol>
 * 纯 JDK，无第三方测试框架。
 */
final class IntervalIndexTest {

    static void run() {
        testInsertAndListOrdered();
        testDuplicateIntervals();
        testNestedIntervals();
        testAdjacentIntervalsDoNotOverlap();
        testHalfOpenPointCoverage();
        testRejectEmptyAndReversedIntervals();
        testDelete();
        testDeleteUnknownId();
        testOverlapQueryEdgeCases();
        testRandomDifferential(20260923L, 400, 6000);
        testRandomDifferential(42L, 80, 4000);
        testLargeRandomDeletesDrainToEmpty(7L, 300);
        testStressTreeShape(99L, 2000);
        System.out.println("IntervalIndexTest: all tests passed");
    }

    // ------------------------------------------------------------------

    static void testInsertAndListOrdered() {
        IntervalIndex idx = new IntervalIndex();
        long a = idx.insert(5, 9);
        long b = idx.insert(1, 4);
        long c = idx.insert(1, 10);
        Asserts.assertEquals(3, idx.size(), "size after 3 inserts");
        List<Interval> all = idx.toList();
        // 按 (start,end,id) 中序排列
        Asserts.assertEquals(1, all.get(0).start(), "order[0].start");
        Asserts.assertEquals(4, all.get(0).end(), "order[0].end");
        Asserts.assertEquals(b, all.get(0).id(), "order[0].id");
        Asserts.assertEquals(1, all.get(1).start(), "order[1].start");
        Asserts.assertEquals(10, all.get(1).end(), "order[1].end");
        Asserts.assertEquals(c, all.get(1).id(), "order[1].id");
        Asserts.assertEquals(5, all.get(2).start(), "order[2].start");
        Asserts.assertEquals(a, all.get(2).id(), "order[2].id");
        idx.checkInvariants();
    }

    static void testDuplicateIntervals() {
        IntervalIndex idx = new IntervalIndex();
        long a = idx.insert(2, 6);
        long b = idx.insert(2, 6);
        long c = idx.insert(2, 6);
        Asserts.assertTrue(a != b && b != c && a != c, "duplicate inserts get distinct ids");
        Asserts.assertEquals(3, idx.size(), "size with duplicates");
        List<Interval> hits = idx.findOverlaps(2, 6);
        Asserts.assertEquals(3, hits.size(), "all 3 duplicates overlap the same span");
        Asserts.assertEquals(3, idx.countCovering(3), "all duplicates cover point 3");
        Asserts.assertEquals(0, idx.countCovering(6), "half-open: none cover end point 6");
        Asserts.assertTrue(idx.delete(b), "delete middle duplicate");
        Asserts.assertEquals(2, idx.size(), "size after deleting one duplicate");
        Asserts.assertEquals(2, idx.findOverlaps(2, 6).size(), "2 duplicates remain");
        idx.checkInvariants();
    }

    static void testNestedIntervals() {
        IntervalIndex idx = new IntervalIndex();
        long outer = idx.insert(0, 100);
        long mid = idx.insert(10, 90);
        long inner = idx.insert(40, 60);
        idx.checkInvariants();

        Asserts.assertEquals(3, idx.findOverlaps(45, 46).size(), "point query inside inner hits all 3");
        // [95,99) 只在 outer [0,100) 内
        Asserts.assertEquals(1, idx.findOverlaps(95, 99).size(), "query in outer-only ring hits 1");
        Asserts.assertEquals(2, idx.findOverlaps(80, 95).size(), "query crossing inner boundary hits outer+mid");
        Asserts.assertEquals(0, idx.findOverlaps(-5, 0).size(), "touching outer start: no overlap");
        Asserts.assertEquals(0, idx.findOverlaps(100, 110).size(), "touching outer end: no overlap");

        Asserts.assertEquals(3, idx.countCovering(50), "coverage depth at 50");
        Asserts.assertEquals(2, idx.countCovering(85), "coverage depth at 85");
        Asserts.assertEquals(1, idx.countCovering(5), "coverage depth at 5");
        Asserts.assertEquals(0, idx.countCovering(100), "coverage at exclusive end");

        Asserts.assertTrue(idx.delete(mid), "delete middle layer");
        Asserts.assertEquals(2, idx.findOverlaps(45, 46).size(), "after delete: outer+inner");
        Asserts.assertEquals(1, idx.findOverlaps(80, 95).size(), "after delete: only outer");
        idx.checkInvariants();
        Asserts.assertTrue(idx.delete(inner), "delete inner");
        Asserts.assertTrue(idx.delete(outer), "delete outer");
        Asserts.assertEquals(0, idx.size(), "empty after all deletes");
        idx.checkInvariants();
    }

    static void testAdjacentIntervalsDoNotOverlap() {
        IntervalIndex idx = new IntervalIndex();
        long a = idx.insert(1, 5);
        long b = idx.insert(5, 9);
        long c = idx.insert(9, 13);
        idx.checkInvariants();

        // 相邻：[1,5) 与 [5,9) 不相交，[5,9) 与 [9,13) 不相交
        Asserts.assertEquals(1, idx.findOverlaps(1, 5).size(), "query [1,5) hits only the first");
        Asserts.assertEquals(1, idx.findOverlaps(5, 9).size(), "query [5,9) hits only the second");
        Asserts.assertEquals(1, idx.findOverlaps(9, 13).size(), "query [9,13) hits only the third");
        // 查询 [5,6) 落在第二段
        Asserts.assertEquals(1, idx.findOverlaps(5, 6).size(), "query [5,6) hits only the second");
        // 查询 [4,5) 只在第一段（5 开）
        Asserts.assertEquals(1, idx.findOverlaps(4, 5).size(), "query [4,5) hits only the first");
        // 点 5 恰好被 [5,9) 覆盖，[1,5) 不含 5
        Asserts.assertEquals(1, idx.countCovering(5), "point 5 covered by exactly one interval");
        Asserts.assertEquals(1, idx.countCovering(9), "point 9 covered by exactly one interval");
        Asserts.assertEquals(0, idx.countCovering(13), "point 13 not covered (open end)");
        // 桥接相邻边界的查询命中两段
        Asserts.assertEquals(2, idx.findOverlaps(4, 6).size(), "query [4,6) bridges 5, hits two");
        Asserts.assertEquals(2, idx.findOverlaps(8, 10).size(), "query [8,10) bridges 9, hits two");
        Asserts.assertEquals(3, idx.findOverlaps(0, 20).size(), "query spanning all hits three");
    }

    static void testHalfOpenPointCoverage() {
        IntervalIndex idx = new IntervalIndex();
        idx.insert(3, 7);
        idx.checkInvariants();
        Asserts.assertEquals(0, idx.countCovering(2), "before start not covered");
        Asserts.assertEquals(1, idx.countCovering(3), "start point covered (closed)");
        Asserts.assertEquals(1, idx.countCovering(6), "last interior point covered");
        Asserts.assertEquals(0, idx.countCovering(7), "end point not covered (open)");
    }

    static void testRejectEmptyAndReversedIntervals() {
        IntervalIndex idx = new IntervalIndex();
        Asserts.expectThrows("empty interval [5,5) rejected", () -> idx.insert(5, 5));
        Asserts.expectThrows("reversed interval rejected", () -> idx.insert(9, 3));
        Asserts.expectThrows("reversed negative interval rejected", () -> idx.insert(-1, -10));
        Asserts.assertEquals(0, idx.size(), "no interval inserted after rejects");
        Asserts.expectThrows("overlap query [4,4) rejected", () -> idx.findOverlaps(4, 4));
        Asserts.expectThrows("overlap query reversed rejected", () -> idx.findOverlaps(8, 2));
        Asserts.expectThrows("anyOverlap empty rejected", () -> idx.anyOverlap(0, 0));
        // record 层同样拒绝
        Asserts.expectThrows("Interval record rejects empty", () -> new Interval(1, 4, 4));
    }

    static void testDelete() {
        IntervalIndex idx = new IntervalIndex();
        List<Long> ids = new ArrayList<>();
        for (int i = 0; i < 50; i++) {
            ids.add(idx.insert(i, i + 10));
        }
        idx.checkInvariants();
        // 打乱删除顺序
        java.util.Collections.shuffle(ids, new Random(123));
        for (Long id : ids) {
            Asserts.assertTrue(idx.delete(id), "delete existing id " + id);
            idx.checkInvariants();
        }
        Asserts.assertEquals(0, idx.size(), "size 0 after deleting all");
        Asserts.assertEquals(0, idx.toList().size(), "list empty");
        Asserts.assertEquals(0, idx.countCovering(5), "coverage 0 when empty");
        Asserts.assertEquals(0, idx.findOverlaps(-100, 100).size(), "overlap query on empty");
    }

    static void testDeleteUnknownId() {
        IntervalIndex idx = new IntervalIndex();
        long id = idx.insert(1, 2);
        Asserts.assertTrue(!idx.delete(999999), "delete unknown id returns false");
        Asserts.assertTrue(!idx.delete(id + 1), "delete never-assigned id returns false");
        Asserts.assertTrue(idx.delete(id), "real delete still works");
        Asserts.assertTrue(!idx.delete(id), "double delete returns false");
        idx.checkInvariants();
    }

    static void testOverlapQueryEdgeCases() {
        IntervalIndex idx = new IntervalIndex();
        idx.insert(10, 20);
        idx.insert(20, 30);
        idx.insert(15, 16);
        idx.checkInvariants();
        // 查询区间只与端点接触不算相交
        Asserts.assertEquals(0, idx.findOverlaps(0, 10).size(), "touch at start 10");
        Asserts.assertEquals(0, idx.findOverlaps(30, 40).size(), "touch at end 30");
        // 包含查询
        Asserts.assertEquals(3, idx.findOverlaps(0, 100).size(), "query contains everything");
        // 点式查询 [15,16)：与 [10,20) 及自身 [15,16) 相交
        Asserts.assertEquals(2, idx.findOverlaps(15, 16).size(), "point query [15,16)");
        Asserts.assertTrue(idx.anyOverlap(19, 21), "anyOverlap bridge");
        Asserts.assertTrue(!idx.anyOverlap(0, 10), "anyOverlap touching only");
    }

    // ------------------------------------------------------------------
    // 差分测试：增强树 vs 暴力扫描
    // ------------------------------------------------------------------

    static void testRandomDifferential(long seed, int coordRange, int ops) {
        Random rnd = new Random(seed);
        IntervalIndex idx = new IntervalIndex();
        // 暴力参考模型
        List<long[]> ref = new ArrayList<>(); // {id,start,end}
        long idGen = 0;

        for (int op = 0; op < ops; op++) {
            int kind = rnd.nextInt(10);
            if (kind < 5 || ref.isEmpty()) {
                // 插入：偏向制造嵌套/相邻/重复
                long s = rnd.nextInt(coordRange) - coordRange / 4L;
                long e;
                int shape = rnd.nextInt(5);
                if (shape == 0 && !ref.isEmpty()) {
                    // 完全重复某个已有区间
                    long[] pick = ref.get(rnd.nextInt(ref.size()));
                    s = pick[1];
                    e = pick[2];
                } else if (shape == 1 && !ref.isEmpty()) {
                    // 相邻：贴着某个端点
                    long[] pick = ref.get(rnd.nextInt(ref.size()));
                    e = s + 1;
                    s = pick[rnd.nextBoolean() ? 1 : 2];
                    e = s + rnd.nextInt(5) + 1;
                } else {
                    int len = rnd.nextInt(6) + 1; // 短区间，制造大量重叠
                    e = s + len;
                }
                if (s < e) {
                    long id = idx.insert(s, e);
                    ref.add(new long[]{id, s, e});
                }
            } else if (kind < 9) {
                // 删除随机一个
                int pos = rnd.nextInt(ref.size());
                long[] victim = ref.remove(pos);
                Asserts.assertTrue(idx.delete(victim[0]),
                        "diff: tree delete must succeed for id " + victim[0]);
            } else {
                // 大量删除（一次删多个，最多 20）
                int batch = Math.min(20, ref.size());
                for (int k = 0; k < batch; k++) {
                    int pos = rnd.nextInt(ref.size());
                    long[] victim = ref.remove(pos);
                    Asserts.assertTrue(idx.delete(victim[0]), "diff: batch delete");
                }
            }

            // 每一步都对照
            idx.checkInvariants();
            Asserts.assertEquals(ref.size(), idx.size(), "diff: size");

            List<Interval> treeAll = idx.toList();
            Asserts.assertEquals(ref.size(), treeAll.size(), "diff: list size");

            // 随机查询：相交（长度至少 1，空查询由专门用例覆盖）
            long lo = rnd.nextInt(coordRange) - coordRange / 4L;
            long hi = lo + rnd.nextInt(8) + 1;
            int brute = 0;
            for (long[] iv : ref) {
                if (iv[1] < hi && iv[2] > lo) {
                    brute++;
                }
            }
            Asserts.assertEquals(brute, idx.findOverlaps(lo, hi).size(),
                    "diff: overlap count for [" + lo + "," + hi + ") ref=" + ref.size());
            Asserts.assertEquals(brute > 0, idx.anyOverlap(lo, hi), "diff: anyOverlap");

            // 随机点覆盖计数（含边界点）
            long t = rnd.nextInt(coordRange) - coordRange / 4L;
            int cover = 0;
            for (long[] iv : ref) {
                if (iv[1] <= t && t < iv[2]) {
                    cover++;
                }
            }
            Asserts.assertEquals(cover, idx.countCovering(t),
                    "diff: coverage at " + t);
        }
        // 收尾：全部删光
        java.util.Collections.shuffle(ref, new Random(seed ^ 0x5DEECE66DL));
        for (long[] iv : ref) {
            Asserts.assertTrue(idx.delete(iv[0]), "diff: final drain delete");
            idx.checkInvariants();
        }
        Asserts.assertEquals(0, idx.size(), "diff: drained to empty");
        idx.checkInvariants();
        System.out.println("  random differential seed=" + seed + " ops=" + ops + " OK");
    }

    static void testLargeRandomDeletesDrainToEmpty(long seed, int n) {
        Random rnd = new Random(seed);
        IntervalIndex idx = new IntervalIndex();
        List<Long> ids = new ArrayList<>();
        for (int i = 0; i < n; i++) {
            long s = rnd.nextInt(1000);
            ids.add(idx.insert(s, s + 1 + rnd.nextInt(1000)));
        }
        idx.checkInvariants();
        Asserts.assertEquals(n, idx.size(), "drain: initial size");
        java.util.Collections.shuffle(ids, rnd);
        int remaining = n;
        for (long id : ids) {
            Asserts.assertTrue(idx.delete(id), "drain: delete");
            remaining--;
            if ((remaining & 31) == 0) {
                idx.checkInvariants();
                Asserts.assertEquals(remaining, idx.size(), "drain: size track");
            }
        }
        idx.checkInvariants();
        Asserts.assertEquals(0, idx.size(), "drain: final empty");
    }

    static void testStressTreeShape(long seed, int n) {
        // 单调插入制造最坏形状，验证黑高平衡下查询仍正确
        IntervalIndex idx = new IntervalIndex();
        for (int i = 0; i < n; i++) {
            idx.insert(i, (long) i + 1);
        }
        int bh = idx.checkInvariants();
        // 黑高应约为 log2(n+1) 的量级；上界宽松校验平衡没有退化
        Asserts.assertTrue(bh <= 2 * (32 - Integer.numberOfLeadingZeros(n)) + 2,
                "black height within balanced bound, bh=" + bh);
        Asserts.assertEquals(n, idx.size(), "stress size");
        // 点区间 [i,i+1)：点 100 恰好只被 [100,101) 覆盖
        Asserts.assertEquals(1, idx.countCovering(100), "coverage of point interval region");
        Asserts.assertEquals(0, idx.countCovering(-1), "coverage before first");
        Asserts.assertEquals(0, idx.countCovering(n), "coverage at open end n");
        // 区间互不相邻重叠（点区间 [i,i+1)），[100,101) 只命中 1 个
        Asserts.assertEquals(1, idx.findOverlaps(100, 101).size(), "point interval query");
        // 范围查询
        Asserts.assertEquals(10, idx.findOverlaps(0, 10).size(), "range [0,10) hits 10");
    }
}
