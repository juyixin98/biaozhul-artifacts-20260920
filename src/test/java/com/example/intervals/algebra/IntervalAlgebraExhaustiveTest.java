package com.example.intervals.algebra;

import com.example.intervals.model.Interval;
import com.example.intervals.model.IntervalSet;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import java.util.BitSet;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;

/**
 * Exhaustive verification over a finite 3-value universe.
 *
 * <p>The universe has {@code 2*3+1 = 7} atomic cells and therefore
 * {@code 2^7 = 128} distinct point sets. Every pair of sets (128 x 128 =
 * 16,384 combinations) is evaluated for union, intersection and difference by
 * both the cut-sweep {@link IntervalAlgebra} and an independent
 * {@link BitSet} oracle, plus complement for every single set. This is the
 * "exhaust every endpoint relation on a finite small domain" acceptance test.
 */
class IntervalAlgebraExhaustiveTest {

    private static final FiniteUniverse<Integer> U =
            new FiniteUniverse<>(List.of(1, 2, 3));

    private IntervalSet<Integer> algebraSet(BitSet cells) {
        List<Interval<Integer>> intervals = U.toIntervals(cells);
        IntervalAlgebra<Integer> algebra = new IntervalAlgebra<>();
        // Normalize the run-length reconstruction (already canonical, but this
        // also exercises normalize on non-trivial input).
        return algebra.normalize(intervals);
    }

    private BitSet actualCells(IntervalSet<Integer> set) {
        return U.toCells(set.intervals());
    }

    @Test
    @DisplayName("exhaustive union/intersection/difference/complement on 7-cell universe")
    void exhaustiveAgainstBitSetOracle() {
        IntervalAlgebra<Integer> algebra = new IntervalAlgebra<>();
        int n = U.cellCount;
        int total = 1 << n;

        for (long am = 0; am < total; am++) {
            BitSet aCells = FiniteUniverse.bitsOf(am, n);
            IntervalSet<Integer> a = algebraSet(aCells);

            // Unary: complement against whole-line oracle flip.
            BitSet expectComp = U.allCells();
            expectComp.andNot(aCells);
            assertEquals(expectComp, actualCells(algebra.complement(a)),
                    "complement mismatch for A=" + aCells);

            // Normalization must be the identity on an already-canonical set.
            assertEquals(a, algebra.normalize(a.intervals()),
                    "normalize not idempotent for A=" + aCells);

            for (long bm = 0; bm < total; bm++) {
                BitSet bCells = FiniteUniverse.bitsOf(bm, n);
                IntervalSet<Integer> b = algebraSet(bCells);

                BitSet expectUnion = (BitSet) aCells.clone();
                expectUnion.or(bCells);
                assertEquals(expectUnion, actualCells(algebra.union(a, b)),
                        "union mismatch for A=" + aCells + " B=" + bCells);

                BitSet expectInter = (BitSet) aCells.clone();
                expectInter.and(bCells);
                assertEquals(expectInter, actualCells(algebra.intersection(a, b)),
                        "intersection mismatch for A=" + aCells + " B=" + bCells);

                BitSet expectDiff = (BitSet) aCells.clone();
                expectDiff.andNot(bCells);
                assertEquals(expectDiff, actualCells(algebra.difference(a, b)),
                        "difference mismatch for A=" + aCells + " B=" + bCells);
            }
        }
    }

    @Test
    @DisplayName("output is disjoint and sorted for every input pair")
    void outputIsNormalizedAndSorted() {
        IntervalAlgebra<Integer> algebra = new IntervalAlgebra<>();
        int n = U.cellCount;
        for (long am = 0; am < (1 << n); am++) {
            IntervalSet<Integer> a = algebraSet(FiniteUniverse.bitsOf(am, n));
            for (long bm = 0; bm < (1 << n); bm++) {
                IntervalSet<Integer> b = algebraSet(FiniteUniverse.bitsOf(bm, n));
                for (IntervalSet<Integer> r : List.of(
                        algebra.union(a, b),
                        algebra.intersection(a, b),
                        algebra.difference(a, b),
                        algebra.complement(a))) {
                    assertSortedAndDisjoint(r);
                }
            }
        }
    }

    private void assertSortedAndDisjoint(IntervalSet<Integer> set) {
        List<Interval<Integer>> ivs = set.intervals();
        for (int i = 1; i < ivs.size(); i++) {
            Interval<Integer> prev = ivs.get(i - 1);
            Interval<Integer> cur = ivs.get(i);
            // Strictly sorted: previous upper cut must be strictly below current
            // lower cut. If they merely touched (gap of nothing), normalization
            // would have merged them.
            if (prev.upperCut().compareTo(cur.lowerCut()) >= 0) {
                throw new AssertionError("intervals not disjoint/sorted: " + ivs);
            }
        }
    }
}
