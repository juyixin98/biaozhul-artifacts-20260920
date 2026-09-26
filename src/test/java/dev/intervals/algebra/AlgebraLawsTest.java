package dev.intervals.algebra;

import dev.intervals.domain.IntervalAlgebra;
import dev.intervals.model.Edge;
import dev.intervals.model.Interval;
import dev.intervals.model.IntervalSet;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.List;

import static dev.intervals.algebra.ExhaustiveFixtures.ep;
import static dev.intervals.algebra.ExhaustiveFixtures.fullSet;
import static dev.intervals.algebra.ExhaustiveFixtures.setOf;
import static org.junit.jupiter.api.Assertions.assertEquals;

/**
 * 布尔代数恒等式：用若干固定集合（含单点、开闭混合、无穷范围）逐一验证。
 */
class AlgebraLawsTest {

    private static final IntervalSet EMPTY = IntervalSet.empty();
    private static final IntervalSet FULL = fullSet();

    // 含：左闭右开、单点、左无穷、缺口
    private static final IntervalSet A = setOf(
            Interval.of(ep(1), Edge.CLOSED, ep(4), Edge.OPEN),
            Interval.singleton(ep(6)),
            Interval.of(ep(8), Edge.OPEN, ep(10), Edge.CLOSED));

    private static final IntervalSet B = setOf(
            Interval.of(ep(0), Edge.OPEN, ep(3), Edge.CLOSED),
            Interval.singleton(ep(6)),
            Interval.of(ep(9), Edge.CLOSED, dev.intervals.model.Endpoint.posInf(), Edge.OPEN));

    private static final IntervalSet C = setOf(
            Interval.of(dev.intervals.model.Endpoint.negInf(), Edge.OPEN, ep(2), Edge.OPEN),
            Interval.of(ep(5), Edge.CLOSED, ep(5), Edge.CLOSED),
            Interval.of(ep(7), Edge.OPEN, ep(9), Edge.OPEN));

    private static final List<IntervalSet> SETS = List.of(EMPTY, FULL, A, B, C);

    @Test
    @DisplayName("同一律：A∪∅=A, A∩U=A")
    void identityLaws() {
        for (IntervalSet x : SETS) {
            assertEquals(x, IntervalAlgebra.union(x, EMPTY));
            assertEquals(x, IntervalAlgebra.intersection(x, FULL));
        }
    }

    @Test
    @DisplayName("支配/零一律：A∪U=U, A∩∅=∅")
    void dominationLaws() {
        for (IntervalSet x : SETS) {
            assertEquals(FULL, IntervalAlgebra.union(x, FULL));
            assertEquals(EMPTY, IntervalAlgebra.intersection(x, EMPTY));
        }
    }

    @Test
    @DisplayName("双重否定：¬¬A=A")
    void doubleComplement() {
        for (IntervalSet x : SETS) {
            assertEquals(x, IntervalAlgebra.complement(IntervalAlgebra.complement(x)),
                    "double complement of " + x);
        }
    }

    @Test
    @DisplayName("补集律：A∪¬A=U, A∩¬A=∅")
    void complementLaws() {
        for (IntervalSet x : SETS) {
            assertEquals(FULL, IntervalAlgebra.union(x, IntervalAlgebra.complement(x)));
            assertEquals(EMPTY, IntervalAlgebra.intersection(x, IntervalAlgebra.complement(x)));
        }
    }

    @Test
    @DisplayName("交换律：A∪B=B∪A, A∩B=B∩A")
    void commutativeLaws() {
        for (IntervalSet x : SETS) {
            for (IntervalSet y : SETS) {
                assertEquals(IntervalAlgebra.union(x, y), IntervalAlgebra.union(y, x));
                assertEquals(IntervalAlgebra.intersection(x, y),
                        IntervalAlgebra.intersection(y, x));
            }
        }
    }

    @Test
    @DisplayName("结合律：(A∪B)∪C=A∪(B∪C), 交同理")
    void associativeLaws() {
        for (IntervalSet x : SETS) {
            for (IntervalSet y : SETS) {
                for (IntervalSet z : SETS) {
                    assertEquals(
                            IntervalAlgebra.union(IntervalAlgebra.union(x, y), z),
                            IntervalAlgebra.union(x, IntervalAlgebra.union(y, z)));
                    assertEquals(
                            IntervalAlgebra.intersection(IntervalAlgebra.intersection(x, y), z),
                            IntervalAlgebra.intersection(x, IntervalAlgebra.intersection(y, z)));
                }
            }
        }
    }

    @Test
    @DisplayName("分配律：A∩(B∪C)=(A∩B)∪(A∩C), A∪(B∩C)=(A∪B)∩(A∪C)")
    void distributiveLaws() {
        for (IntervalSet x : SETS) {
            for (IntervalSet y : SETS) {
                for (IntervalSet z : SETS) {
                    IntervalSet lhs1 = IntervalAlgebra.intersection(
                            x, IntervalAlgebra.union(y, z));
                    IntervalSet rhs1 = IntervalAlgebra.union(
                            IntervalAlgebra.intersection(x, y),
                            IntervalAlgebra.intersection(x, z));
                    assertEquals(lhs1, rhs1);

                    IntervalSet lhs2 = IntervalAlgebra.union(
                            x, IntervalAlgebra.intersection(y, z));
                    IntervalSet rhs2 = IntervalAlgebra.intersection(
                            IntervalAlgebra.union(x, y),
                            IntervalAlgebra.union(x, z));
                    assertEquals(lhs2, rhs2);
                }
            }
        }
    }

    @Test
    @DisplayName("德摩根：¬(A∪B)=¬A∩¬B, ¬(A∩B)=¬A∪¬B")
    void deMorganLaws() {
        for (IntervalSet x : SETS) {
            for (IntervalSet y : SETS) {
                assertEquals(
                        IntervalAlgebra.complement(IntervalAlgebra.union(x, y)),
                        IntervalAlgebra.intersection(
                                IntervalAlgebra.complement(x), IntervalAlgebra.complement(y)));
                assertEquals(
                        IntervalAlgebra.complement(IntervalAlgebra.intersection(x, y)),
                        IntervalAlgebra.union(
                                IntervalAlgebra.complement(x), IntervalAlgebra.complement(y)));
            }
        }
    }

    @Test
    @DisplayName("吸收律与幂等律")
    void absorptionAndIdempotence() {
        for (IntervalSet x : SETS) {
            for (IntervalSet y : SETS) {
                assertEquals(x, IntervalAlgebra.union(x, IntervalAlgebra.intersection(x, y)));
                assertEquals(x, IntervalAlgebra.intersection(x, IntervalAlgebra.union(x, y)));
            }
            assertEquals(x, IntervalAlgebra.union(x, x));
            assertEquals(x, IntervalAlgebra.intersection(x, x));
            assertEquals(EMPTY, IntervalAlgebra.difference(x, x));
        }
    }

    @Test
    @DisplayName("差集恒等式：A−B=A∩¬B")
    void differenceIsIntersectComplement() {
        for (IntervalSet x : SETS) {
            for (IntervalSet y : SETS) {
                assertEquals(
                        IntervalAlgebra.difference(x, y),
                        IntervalAlgebra.intersection(x, IntervalAlgebra.complement(y)));
            }
        }
    }

    @Test
    @DisplayName("穷举恒等式：对域内随机生成集合子集验证 A∪¬A=U 与双重否定")
    void exhaustiveSubsetLaws() {
        // 枚举 {0..4} 上 2^5=32 个点集，每个点集用单点子集的并表示
        List<IntervalSet> all = new ArrayList<>();
        for (int mask = 0; mask < (1 << 5); mask++) {
            List<Interval> parts = new ArrayList<>();
            for (int p = 0; p < 5; p++) {
                if ((mask & (1 << p)) != 0) {
                    parts.add(Interval.singleton(ep(p)));
                }
            }
            all.add(IntervalSet.of(parts));
        }
        for (IntervalSet x : all) {
            assertEquals(x, IntervalAlgebra.complement(IntervalAlgebra.complement(x)));
            assertEquals(FULL, IntervalAlgebra.union(x, IntervalAlgebra.complement(x)));
            assertEquals(EMPTY, IntervalAlgebra.intersection(x, IntervalAlgebra.complement(x)));
        }
    }
}
