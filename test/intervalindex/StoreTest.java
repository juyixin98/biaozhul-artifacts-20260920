package intervalindex;

import java.util.List;

/** 针对验收要点的定向测试：嵌套区间、相邻端点、重复区间、交集查询与覆盖计数。 */
public class StoreTest {

    public static void run() {
        nestedIntervals();
        adjacentEndpoints();
        duplicateIntervals();
        overlapEdgeCases();
        largeEndpoints();
        System.out.println("StoreTest OK");
    }

    private static List<long[]> toPairs(List<Interval> hits) {
        return hits.stream().map(i -> new long[]{i.lo(), i.hi()}).toList();
    }

    private static void nestedIntervals() {
        IntervalStore s = new IntervalStore();
        s.insert(0, 100);
        s.insert(10, 90);
        s.insert(20, 80);
        s.insert(30, 70);
        s.insert(40, 60);

        Check.eq(s.coverage(5), 1, "nested coverage t=5");
        Check.eq(s.coverage(45), 5, "nested coverage t=45: all five");
        Check.eq(s.coverage(59), 5, "nested coverage t=59");
        Check.eq(s.coverage(60), 4, "nested coverage t=60: [40,60) ended");
        Check.eq(s.coverage(90), 1, "nested coverage t=90: only outer remains");
        Check.eq(s.coverage(100), 0, "nested coverage t=100: right-open");

        // 查询最内层小区间：应命中全部五个嵌套区间
        List<long[]> hits = toPairs(s.queryOverlap(45, 55));
        Check.eq(hits.size(), 5, "inner query hits all nested");

        // 查询仅落在外壳上、不进入第二层的区域
        hits = toPairs(s.queryOverlap(0, 10));
        Check.eq(hits.size(), 1, "outer shell only hits outer (right-open at 10)");

        // 删除最外层后，内部结构不变
        Check.eq(s.delete(0, 100, 1), 1, "delete outer");
        Check.eq(s.coverage(45), 4, "coverage 4 after outer removed");
        TreeInvariants.verifyBoth(s, 4);

        // 删除中间层
        Check.eq(s.delete(20, 80, 1), 1, "delete middle");
        hits = toPairs(s.queryOverlap(25, 35));
        Check.eq(hits.size(), 2, "after middle delete: [10,90) and [30,70)");
        TreeInvariants.verifyBoth(s, 3);
    }

    private static void adjacentEndpoints() {
        IntervalStore s = new IntervalStore();
        s.insert(0, 5);
        s.insert(5, 10);
        s.insert(10, 15);

        // 端点处：左闭右开，恰好相邻的区间不相交、覆盖计数不重复
        Check.eq(s.coverage(5), 1, "coverage at boundary 5 = only [5,10)");
        Check.eq(s.coverage(10), 1, "coverage at boundary 10 = only [10,15)");
        Check.eq(s.coverage(4), 1, "coverage at 4");
        Check.eq(s.coverage(15), 0, "coverage at 15 (all right-open-ended)");

        // 相邻区间互相不算重叠
        Check.eq(s.queryOverlap(0, 5).size(), 1, "[0,5) query: [5,10) is adjacent, not overlapping");
        Check.eq(s.queryOverlap(5, 10).size(), 1, "[5,10) exact");
        Check.eq(s.queryOverlap(-5, 0).size(), 0, "query before everything");
        Check.eq(s.queryOverlap(15, 20).size(), 0, "query after everything");

        // 查询区间跨越两个相邻区间
        Check.eq(s.queryOverlap(3, 8).size(), 2, "query straddles 5");
        Check.eq(s.queryOverlap(0, 15).size(), 3, "query spans all");
    }

    private static void duplicateIntervals() {
        IntervalStore s = new IntervalStore();
        for (int i = 0; i < 5; i++) {
            s.insert(2, 8);
        }
        Check.eq(s.distinctCount(), 1, "dup distinct");
        Check.eq(s.totalCount(), 5, "dup total");
        Check.eq(s.coverage(3), 5, "dup coverage");
        Check.eq(s.queryOverlap(0, 100).size(), 5, "dup overlap expands copies");

        // 同一区间与其他区间混合
        s.insert(2, 8); // 6 份
        s.insert(0, 3);
        Check.eq(s.coverage(2), 7, "t=2: six [2,8) + one [0,3)");
        Check.eq(s.coverage(3), 6, "t=3: [0,3) ended");

        Check.eq(s.delete(2, 8, 4), 4, "delete four of six");
        Check.eq(s.coverage(2), 3, "t=2 after delete: 2 + 1");
        Check.eq(s.delete(2, 8, 4), 2, "delete remaining two (overshoot)");
        Check.eq(s.coverage(2), 1, "only [0,3) left");
    }

    private static void overlapEdgeCases() {
        IntervalStore s = new IntervalStore();
        s.insert(-10, -5);
        s.insert(-5, 0);
        s.insert(0, 5);
        s.insert(3, 10);

        // 单点式查询非法（空区间）
        try {
            s.queryOverlap(3, 3);
            Check.fail("empty query interval must be rejected");
        } catch (IllegalArgumentException expected) {
            // expected
        }
        try {
            s.queryOverlap(9, 2);
            Check.fail("reversed query interval must be rejected");
        } catch (IllegalArgumentException expected) {
            // expected
        }

        // 负数区间正常工作
        Check.eq(s.queryOverlap(-7, -6).size(), 1, "negative interval hit");
        Check.eq(s.queryOverlap(-6, -5).size(), 1, "right-open: [-5,0) not hit by query ending -5");
        Check.eq(s.queryOverlap(-5, 0).size(), 1, "exact negative interval");
        Check.eq(s.queryOverlap(-20, 20).size(), 4, "everything");

        // 排序输出
        List<Interval> hits = s.queryOverlap(-20, 20);
        for (int i = 1; i < hits.size(); i++) {
            Interval a = hits.get(i - 1);
            Interval b = hits.get(i);
            Check.that(a.lo() < b.lo() || (a.lo() == b.lo() && a.hi() <= b.hi()),
                    "overlap result must be sorted by (lo,hi)");
        }
    }

    private static void largeEndpoints() {
        IntervalStore s = new IntervalStore();
        s.insert(Long.MIN_VALUE, Long.MAX_VALUE);
        s.insert(Long.MIN_VALUE, 0);
        s.insert(0, Long.MAX_VALUE);
        Check.eq(s.coverage(0), 2, "coverage at 0: [MIN,0) already ended (right-open)");
        Check.eq(s.coverage(Long.MIN_VALUE), 2, "coverage at MIN_VALUE: two intervals start there");
        Check.eq(s.coverage(-1), 2, "coverage at -1: outer + [MIN,0)");
        Check.eq(s.coverage(Long.MAX_VALUE - 1), 2, "coverage near MAX_VALUE: outer + [0,MAX)");
        Check.eq(s.coverage(Long.MAX_VALUE), 0, "coverage at MAX_VALUE: right-open");
        Check.eq(s.queryOverlap(Long.MIN_VALUE, 0).size(), 2,
                "MIN..0 hits [MIN,MAX) and [MIN,0) (not [0,MAX))");
        TreeInvariants.verifyBoth(s, 3);
    }
}
