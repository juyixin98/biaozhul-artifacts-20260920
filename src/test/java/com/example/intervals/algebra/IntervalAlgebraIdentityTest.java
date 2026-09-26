package com.example.intervals.algebra;

import com.example.intervals.model.Interval;
import com.example.intervals.model.IntervalSet;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import java.util.BitSet;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;

/**
 * Boolean-algebra identity checks, exhaustively over every triple of point
 * sets in the finite 7-cell universe. These identities are precisely the
 * contract of a set-algebra service.
 */
class IntervalAlgebraIdentityTest {

    private static final FiniteUniverse<Integer> U =
            new FiniteUniverse<>(List.of(1, 2, 3));

    private final IntervalAlgebra<Integer> alg = new IntervalAlgebra<>();

    private IntervalSet<Integer> setOf(long mask) {
        BitSet cells = FiniteUniverse.bitsOf(mask, U.cellCount);
        return alg.normalize(U.toIntervals(cells));
    }

    private BitSet cells(IntervalSet<Integer> s) {
        return U.toCells(s.intervals());
    }

    private BitSet mask(long m) {
        return FiniteUniverse.bitsOf(m, U.cellCount);
    }

    @Test
    @DisplayName("De Morgan: comp(A∪B)=comp(A)∩comp(B), comp(A∩B)=comp(A)∪comp(B)")
    void deMorgan() {
        for (long a = 0; a < 128; a++) {
            for (long b = 0; b < 128; b++) {
                IntervalSet<Integer> A = setOf(a);
                IntervalSet<Integer> B = setOf(b);

                assertEquals(
                        cells(alg.complement(alg.union(A, B))),
                        cells(alg.intersection(alg.complement(A), alg.complement(B))));
                assertEquals(
                        cells(alg.complement(alg.intersection(A, B))),
                        cells(alg.union(alg.complement(A), alg.complement(B))));
            }
        }
    }

    @Test
    @DisplayName("distributivity: A∩(B∪C)=(A∩B)∪(A∩C), A∪(B∩C)=(A∪B)∩(A∪C)")
    void distributivity() {
        for (long a = 0; a < 128; a++) {
            for (long b = 0; b < 128; b++) {
                for (long c = 0; c < 128; c++) {
                    IntervalSet<Integer> A = setOf(a);
                    IntervalSet<Integer> B = setOf(b);
                    IntervalSet<Integer> C = setOf(c);

                    assertEquals(
                            cells(alg.intersection(A, alg.union(B, C))),
                            cells(alg.union(alg.intersection(A, B), alg.intersection(A, C))));
                    assertEquals(
                            cells(alg.union(A, alg.intersection(B, C))),
                            cells(alg.intersection(alg.union(A, B), alg.union(A, C))));
                }
            }
        }
    }

    @Test
    @DisplayName("double complement involution and A−B = A∩comp(B)")
    void involutionAndDifferenceDefinition() {
        for (long a = 0; a < 128; a++) {
            for (long b = 0; b < 128; b++) {
                IntervalSet<Integer> A = setOf(a);
                IntervalSet<Integer> B = setOf(b);
                assertEquals(cells(A), cells(alg.complement(alg.complement(A))));
                assertEquals(
                        cells(alg.difference(A, B)),
                        cells(alg.intersection(A, alg.complement(B))));
            }
        }
    }

    @Test
    @DisplayName("absorption, identity and annihilator laws")
    void absorptionIdentityAnnihilator() {
        IntervalSet<Integer> empty = alg.normalize(List.of());
        IntervalSet<Integer> universe = alg.complement(empty);

        for (long a = 0; a < 128; a++) {
            IntervalSet<Integer> A = setOf(a);
            // absorption
            assertEquals(cells(A), cells(alg.union(A, alg.intersection(A, A))));
            // identity
            assertEquals(cells(A), cells(alg.union(A, empty)));
            assertEquals(cells(A), cells(alg.intersection(A, universe)));
            // annihilator
            assertEquals(cells(empty), cells(alg.intersection(A, empty)));
            assertEquals(cells(universe), cells(alg.union(A, universe)));
            // complement
            assertEquals(cells(empty), cells(alg.intersection(A, alg.complement(A))));
            assertEquals(cells(universe), cells(alg.union(A, alg.complement(A))));
        }
    }

    @Test
    @DisplayName("commutativity of union/intersection (normalized equality)")
    void commutativity() {
        for (long a = 0; a < 128; a++) {
            for (long b = 0; b < 128; b++) {
                IntervalSet<Integer> A = setOf(a);
                IntervalSet<Integer> B = setOf(b);
                assertEquals(alg.union(A, B), alg.union(B, A));
                assertEquals(alg.intersection(A, B), alg.intersection(B, A));
            }
        }
    }
}
