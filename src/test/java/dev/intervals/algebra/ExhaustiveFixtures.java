package dev.intervals.algebra;

import dev.intervals.domain.IntervalAlgebra;
import dev.intervals.model.Edge;
import dev.intervals.model.Endpoint;
import dev.intervals.model.Interval;
import dev.intervals.model.IntervalSet;

import java.util.ArrayList;
import java.util.BitSet;
import java.util.Collections;
import java.util.List;

/**
 * 穷举测试共享工具：
 *
 * <p>在有限小域 D = {0..7} 上生成所有合法有限区间（含 4 种开闭组合与单点），
 * 以及一端/两端无穷区间。参考真值用截断全集 U = {-10..17} 上的点集（{@link BitSet}）
 * 表示——输入端点均远离截断边界，故 U 上的成员关系与真实整数轴完全一致，
 * 补集在 U 上也与真实补集逐点一致。
 */
final class ExhaustiveFixtures {

    static final int LO = -10;
    static final int HI = 17;
    static final int DOMAIN_MIN = 0;
    static final int DOMAIN_MAX = 7;

    private ExhaustiveFixtures() {
    }

    /** 所有待穷举的合法区间（有限 + 无穷）。 */
    static List<Interval> allIntervals() {
        List<Interval> list = new ArrayList<>();
        for (int a = DOMAIN_MIN; a <= DOMAIN_MAX; a++) {
            list.add(Interval.singleton(ep(a)));
            for (int b = a + 1; b <= DOMAIN_MAX; b++) {
                for (Edge le : Edge.values()) {
                    for (Edge re : Edge.values()) {
                        list.add(Interval.of(ep(a), le, ep(b), re));
                    }
                }
            }
        }
        // 一端无穷
        for (int a = DOMAIN_MIN; a <= DOMAIN_MAX; a++) {
            list.add(Interval.of(Endpoint.negInf(), Edge.OPEN, ep(a), Edge.OPEN));
            list.add(Interval.of(Endpoint.negInf(), Edge.OPEN, ep(a), Edge.CLOSED));
            list.add(Interval.of(ep(a), Edge.OPEN, Endpoint.posInf(), Edge.OPEN));
            list.add(Interval.of(ep(a), Edge.CLOSED, Endpoint.posInf(), Edge.OPEN));
        }
        // 全集
        list.add(Interval.of(Endpoint.negInf(), Edge.OPEN, Endpoint.posInf(), Edge.OPEN));
        return List.copyOf(list);
    }

    static Endpoint ep(int v) {
        return Endpoint.finite(v);
    }

    /** 单个区间 → U 上的点集。 */
    static BitSet points(Interval i) {
        BitSet bs = new BitSet();
        for (int p = LO; p <= HI; p++) {
            if (i.contains(ep(p))) {
                bs.set(index(p));
            }
        }
        return bs;
    }

    /** 规范化集合 → U 上的点集。 */
    static BitSet points(IntervalSet set) {
        BitSet bs = new BitSet();
        for (Interval i : set.intervals()) {
            bs.or(points(i));
        }
        return bs;
    }

    static int index(int p) {
        return p - LO;
    }

    static int universeSize() {
        return HI - LO + 1;
    }

    static BitSet universe() {
        BitSet bs = new BitSet();
        bs.set(0, universeSize());
        return bs;
    }

    static IntervalSet fullSet() {
        return IntervalSet.of(List.of(
                Interval.of(Endpoint.negInf(), Edge.OPEN, Endpoint.posInf(), Edge.OPEN)));
    }

    static IntervalSet setOf(Interval... intervals) {
        List<Interval> l = new ArrayList<>();
        Collections.addAll(l, intervals);
        return IntervalSet.of(l);
    }
}
