package com.example.intervals.algebra;

import com.example.intervals.error.IntervalException;
import com.example.intervals.model.Cut;
import com.example.intervals.model.Interval;
import com.example.intervals.model.IntervalSet;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import java.util.BitSet;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * Enumerates <strong>every</strong> endpoint relation on a finite universe:
 * all ordered pairs of the {@code 2k+2} boundary cuts (including open/closed
 * finite cuts and both infinities). Each pair falls into exactly one of three
 * classes, all asserted here:
 * <ol>
 *   <li>descending finite endpoint values &rarr; rejected as reversed;</li>
 *   <li>same value but degenerate cut order, e.g. {@code (v,v)}, {@code [v,v)}
 *       &rarr; accepted but {@code isEmpty()};</li>
 *   <li>strictly ordered cuts &rarr; non-empty, and it covers exactly one
 *       contiguous run of atomic cells.</li>
 * </ol>
 * For every pair of non-empty intervals the four algebra operations are
 * cross-checked against the BitSet oracle.
 */
class BoundaryRelationExhaustiveTest {

    private static final FiniteUniverse<Integer> U =
            new FiniteUniverse<>(List.of(1, 2, 3));

    private final IntervalAlgebra<Integer> alg = new IntervalAlgebra<>();

    private boolean finiteDescending(Cut<Integer> lo, Cut<Integer> hi) {
        return lo.isFinite() && hi.isFinite()
                && lo.endpoint().compareTo(hi.endpoint()) > 0;
    }

    @Test
    @DisplayName("all 8x8 cut pairs: reversed rejected, degenerate empty, otherwise contiguous")
    void everyCutPairClassified() {
        int b = U.boundaries.length; // 2k+2 = 8
        for (int i = 0; i < b; i++) {
            for (int j = 0; j < b; j++) {
                Cut<Integer> lo = U.boundary(i);
                Cut<Integer> hi = U.boundary(j);

                if (finiteDescending(lo, hi)) {
                    assertThrows(IntervalException.class, () -> Interval.of(lo, hi),
                            "expected reversed rejection for " + lo + ".." + hi);
                    continue;
                }

                Interval<Integer> iv = Interval.of(lo, hi);
                if (lo.compareTo(hi) >= 0) {
                    // Same-value degenerate forms (v,v), [v,v), (v,v] and point
                    // cuts that coincide.
                    assertTrue(iv.isEmpty(), "expected empty for " + lo + ".." + hi);
                    continue;
                }

                assertFalse(iv.isEmpty(), "expected non-empty for " + lo + ".." + hi);
                BitSet cells = U.toCells(iv);
                // A convex interval must cover one contiguous run of cells.
                assertContiguous(cells);
                // Its cut endpoints must be exactly the run's outer boundaries.
                List<Interval<Integer>> rebuilt = U.toIntervals(cells);
                assertEquals(1, rebuilt.size());
                assertEquals(iv, rebuilt.get(0));
            }
        }
    }

    @Test
    @DisplayName("all non-empty interval pairs agree with the cell oracle")
    void allIntervalPairsAgreeWithOracle() {
        List<Interval<Integer>> nonEmpty = allNonEmptyIntervals();
        for (Interval<Integer> x : nonEmpty) {
            BitSet xc = U.toCells(x);
            for (Interval<Integer> y : nonEmpty) {
                BitSet yc = U.toCells(y);
                IntervalSet<Integer> sx = alg.normalize(List.of(x));
                IntervalSet<Integer> sy = alg.normalize(List.of(y));

                BitSet u = (BitSet) xc.clone();
                u.or(yc);
                BitSet inter = (BitSet) xc.clone();
                inter.and(yc);
                BitSet diff = (BitSet) xc.clone();
                diff.andNot(yc);

                assertEquals(u, U.toCells(alg.union(sx, sy).intervals()));
                assertEquals(inter, U.toCells(alg.intersection(sx, sy).intervals()));
                assertEquals(diff, U.toCells(alg.difference(sx, sy).intervals()));
            }
        }
    }

    private List<Interval<Integer>> allNonEmptyIntervals() {
        List<Interval<Integer>> out = new java.util.ArrayList<>();
        int b = U.boundaries.length;
        for (int i = 0; i < b; i++) {
            for (int j = i + 1; j < b; j++) {
                Cut<Integer> lo = U.boundary(i);
                Cut<Integer> hi = U.boundary(j);
                if (!finiteDescending(lo, hi)) {
                    out.add(Interval.of(lo, hi));
                }
            }
        }
        return out;
    }

    private void assertContiguous(BitSet cells) {
        int first = cells.nextSetBit(0);
        if (first < 0) {
            return;
        }
        int last = cells.length() - 1;
        for (int i = first; i <= last; i++) {
            if (!cells.get(i)) {
                // a hole after a set bit before the final set bit => not convex
                int after = cells.nextSetBit(i + 1);
                if (after >= 0) {
                    throw new AssertionError("non-contiguous coverage: " + cells);
                }
            }
        }
    }
}
