package dev.intervals.algebra;

import dev.intervals.domain.IntervalAlgebra;
import dev.intervals.model.Interval;
import dev.intervals.model.IntervalSet;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.BitSet;
import java.util.List;

import static dev.intervals.algebra.ExhaustiveFixtures.HI;
import static dev.intervals.algebra.ExhaustiveFixtures.LO;
import static dev.intervals.algebra.ExhaustiveFixtures.allIntervals;
import static dev.intervals.algebra.ExhaustiveFixtures.ep;
import static dev.intervals.algebra.ExhaustiveFixtures.fullSet;
import static dev.intervals.algebra.ExhaustiveFixtures.points;
import static dev.intervals.algebra.ExhaustiveFixtures.setOf;
import static dev.intervals.algebra.ExhaustiveFixtures.universe;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * 穷举有限小域上的端点关系：所有合法区间两两配对，
 * 与逐点真值（BitSet 参考实现）逐一核对四种运算。
 */
class ExhaustiveTest {

    private static final List<Interval> ALL = allIntervals();

    @Test
    @DisplayName("穷举：单区间 ∪ ∩ − ¬ 与逐点真值一致（含无穷区间）")
    void exhaustivePairs() {
        BitSet u = universe();
        long pairs = 0;
        for (Interval a : ALL) {
            for (Interval b : ALL) {
                IntervalSet A = setOf(a);
                IntervalSet B = setOf(b);
                BitSet pa = points(a);
                BitSet pb = points(b);

                BitSet union = (BitSet) pa.clone();
                union.or(pb);
                BitSet inter = (BitSet) pa.clone();
                inter.and(pb);
                BitSet diff = (BitSet) pa.clone();
                diff.andNot(pb);
                BitSet comp = (BitSet) u.clone();
                comp.andNot(pa);

                assertEquals(union, points(IntervalAlgebra.union(A, B)),
                        "union " + a + " / " + b);
                assertEquals(inter, points(IntervalAlgebra.intersection(A, B)),
                        "intersection " + a + " / " + b);
                assertEquals(diff, points(IntervalAlgebra.difference(A, B)),
                        "difference " + a + " / " + b);
                assertEquals(comp, points(IntervalAlgebra.complement(A)),
                        "complement " + a);
                pairs++;
            }
        }
        assertTrue(pairs > 1000, "穷举规模应覆盖上千对，实际 " + pairs);
    }

    @Test
    @DisplayName("穷举：多区间集合（3 个一组）四种运算与真值一致")
    void exhaustiveSetsOfThree() {
        BitSet u = universe();
        // 采样：每 7 个区间取一个做三元组合，控制运行规模但覆盖全部端点形态
        List<Interval> sample = new ArrayList<>();
        for (int i = 0; i < ALL.size(); i += 7) {
            sample.add(ALL.get(i));
        }
        long cases = 0;
        for (Interval a : sample) {
            for (Interval b : sample) {
                for (Interval c : sample) {
                    IntervalSet A = setOf(a, b);
                    IntervalSet B = setOf(b, c);
                    BitSet pa = points(A);
                    BitSet pb = points(B);

                    BitSet union = (BitSet) pa.clone();
                    union.or(pb);
                    BitSet inter = (BitSet) pa.clone();
                    inter.and(pb);
                    BitSet diff = (BitSet) pa.clone();
                    diff.andNot(pb);
                    BitSet comp = (BitSet) u.clone();
                    comp.andNot(pa);

                    assertEquals(union, points(IntervalAlgebra.union(A, B)));
                    assertEquals(inter, points(IntervalAlgebra.intersection(A, B)));
                    assertEquals(diff, points(IntervalAlgebra.difference(A, B)));
                    assertEquals(comp, points(IntervalAlgebra.complement(A)));
                    cases++;
                }
            }
        }
        assertTrue(cases > 5000, "三元穷举规模 " + cases);
    }

    @Test
    @DisplayName("规范化唯一性：同一集合不同输入顺序/重叠方式规范化结果相同")
    void normalizationIsUnique() {
        Interval a = Interval.of(ep(1), dev.intervals.model.Edge.CLOSED, ep(4),
                dev.intervals.model.Edge.OPEN);
        Interval b = Interval.of(ep(3), dev.intervals.model.Edge.CLOSED, ep(6),
                dev.intervals.model.Edge.CLOSED);
        Interval c = Interval.of(ep(4), dev.intervals.model.Edge.OPEN, ep(6),
                dev.intervals.model.Edge.CLOSED);

        IntervalSet s1 = setOf(a, b);
        IntervalSet s2 = setOf(b, a, a);
        IntervalSet s3 = setOf(
                Interval.of(ep(1), dev.intervals.model.Edge.CLOSED, ep(2),
                        dev.intervals.model.Edge.CLOSED),
                Interval.of(ep(2), dev.intervals.model.Edge.CLOSED, ep(4),
                        dev.intervals.model.Edge.OPEN),
                Interval.singleton(ep(4)),
                c);
        assertEquals(s1, s2);
        assertEquals(s1, s3);
    }

    @Test
    @DisplayName("输出稳定排序：多段结果按下端点升序且互不相交")
    void outputStableSortedAndDisjoint() {
        List<Interval> shuffled = new ArrayList<>(List.of(
                Interval.of(ep(7), dev.intervals.model.Edge.CLOSED, ep(8),
                        dev.intervals.model.Edge.CLOSED),
                Interval.of(ep(1), dev.intervals.model.Edge.OPEN, ep(2),
                        dev.intervals.model.Edge.OPEN),
                Interval.of(ep(4), dev.intervals.model.Edge.CLOSED, ep(5),
                        dev.intervals.model.Edge.OPEN),
                Interval.of(ep(2), dev.intervals.model.Edge.CLOSED, ep(3),
                        dev.intervals.model.Edge.CLOSED)));
        IntervalSet s = IntervalSet.of(shuffled);
        List<Interval> out = s.intervals();
        for (int i = 1; i < out.size(); i++) {
            assertTrue(out.get(i - 1).lower().compareTo(out.get(i).lower()) < 0,
                    "结果必须严格按下端点升序");
        }
        // 段与段之间不允许有共同包含的点（闭-闭相邻应已合并）
        for (int i = 1; i < out.size(); i++) {
            BitSet p1 = points(out.get(i - 1));
            BitSet p2 = points(out.get(i));
            BitSet overlap = (BitSet) p1.clone();
            overlap.and(p2);
            assertTrue(overlap.isEmpty(), "规范化结果段间不得重叠");
        }
    }

    @Test
    @DisplayName("空集：∅ 的并交差为空，补为全集；与非空集合区分")
    void emptySetLaws() {
        IntervalSet empty = IntervalSet.empty();
        IntervalSet full = fullSet();
        IntervalSet x = setOf(Interval.of(ep(2), dev.intervals.model.Edge.CLOSED, ep(5),
                dev.intervals.model.Edge.CLOSED));

        assertEquals(empty, IntervalAlgebra.union(empty, empty));
        assertEquals(empty, IntervalAlgebra.intersection(empty, x));
        assertEquals(empty, IntervalAlgebra.difference(empty, x));
        assertEquals(x, IntervalAlgebra.union(empty, x));
        assertEquals(x, IntervalAlgebra.difference(x, empty));
        assertEquals(full, IntervalAlgebra.complement(empty));
        assertEquals(empty, IntervalAlgebra.complement(full));
        assertNotEquals(empty, full);
    }

    @Test
    @DisplayName("边界扫描：LO..HI 全点覆盖无遗漏（区间端点遍历域内每个整数）")
    void everyPointCoveredByAdjacentIntervals() {
        // [0,3] ∪ [4,7]（闭-闭相邻值）应等于 [0,7]
        IntervalSet merged = IntervalSet.of(List.of(
                Interval.of(ep(0), dev.intervals.model.Edge.CLOSED, ep(3),
                        dev.intervals.model.Edge.CLOSED),
                Interval.of(ep(4), dev.intervals.model.Edge.CLOSED, ep(7),
                        dev.intervals.model.Edge.CLOSED)));
        BitSet expect = new BitSet();
        for (int p = 0; p <= 7; p++) {
            expect.set(ExhaustiveFixtures.index(p));
        }
        assertEquals(expect, points(merged));
        // 但 (0,3] ∪ [4,7) 不包含 0 和 7
        BitSet partial = points(IntervalSet.of(List.of(
                Interval.of(ep(0), dev.intervals.model.Edge.OPEN, ep(3),
                        dev.intervals.model.Edge.CLOSED),
                Interval.of(ep(4), dev.intervals.model.Edge.CLOSED, ep(7),
                        dev.intervals.model.Edge.OPEN))));
        for (int p = LO; p <= HI; p++) {
            assertEquals(p >= 1 && p <= 6,
                    partial.get(ExhaustiveFixtures.index(p)), "点 " + p);
        }
    }
}
