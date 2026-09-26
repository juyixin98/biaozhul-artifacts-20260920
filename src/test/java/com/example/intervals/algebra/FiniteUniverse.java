package com.example.intervals.algebra;

import com.example.intervals.model.Cut;
import com.example.intervals.model.Interval;

import java.util.ArrayList;
import java.util.BitSet;
import java.util.List;

/**
 * Independent finite oracle for {@link IntervalAlgebra}.
 *
 * <p>Given {@code k} ordered endpoint values, the dense domain line is
 * partitioned into {@code 2k+1} atomic cells:
 * <pre>
 *   (-inf,v1), {v1}, (v1,v2), {v2}, ..., {vk}, (vk,+inf)
 * </pre>
 * Any interval whose endpoints are drawn from these values (with open/closed
 * or infinite ends) is exactly a contiguous run of these cells, and any set
 * of such intervals is exactly a {@link BitSet} over them. Set algebra on
 * BitSets (|, &amp;, and-not, flip) is an obviously-correct independent
 * implementation that the cut-sweep engine must agree with for every one of
 * the {@code 2^(2k+1)} point sets.
 *
 * <p>Cuts are indexed as boundaries:
 * {@code C0=-inf, C1=BELOW(v1), C2=ABOVE(v1), ..., C_{2k}=ABOVE(vk),
 * C_{2k+1}=+inf}; cell {@code j} lies between boundaries {@code C_j} and
 * {@code C_{j+1}}.
 */
final class FiniteUniverse<T extends Comparable<? super T>> {

    final List<T> values;
    final int k;
    final int cellCount;
    final Cut<T>[] boundaries;

    @SuppressWarnings("unchecked")
    FiniteUniverse(List<T> orderedValues) {
        this.values = List.copyOf(orderedValues);
        this.k = values.size();
        this.cellCount = 2 * k + 1;
        this.boundaries = new Cut[2 * k + 2];
        this.boundaries[0] = Cut.negInfinity();
        this.boundaries[2 * k + 1] = Cut.posInfinity();
        for (int i = 1; i <= k; i++) {
            T v = values.get(i - 1);
            this.boundaries[2 * i - 1] = Cut.below(v);
            this.boundaries[2 * i] = Cut.above(v);
        }
    }

    Cut<T> boundary(int index) {
        return boundaries[index];
    }

    /** Index of a cut among the canonical boundaries, or -1 if not present. */
    int boundaryIndex(Cut<T> cut) {
        for (int i = 0; i < boundaries.length; i++) {
            if (boundaries[i].equals(cut)) {
                return i;
            }
        }
        return -1;
    }

    /** Encodes one interval as the cells it covers. */
    BitSet toCells(Interval<T> interval) {
        BitSet cells = new BitSet(cellCount);
        for (int j = 0; j < cellCount; j++) {
            boolean covers = interval.lowerCut().compareTo(boundaries[j]) <= 0
                    && interval.upperCut().compareTo(boundaries[j + 1]) >= 0;
            if (covers) {
                cells.set(j);
            }
        }
        return cells;
    }

    /** Encodes a list of intervals as the union of cells they cover. */
    BitSet toCells(List<Interval<T>> intervals) {
        BitSet cells = new BitSet(cellCount);
        for (Interval<T> iv : intervals) {
            cells.or(toCells(iv));
        }
        return cells;
    }

    /** Builds the canonical run-length interval list for a cell subset. */
    List<Interval<T>> toIntervals(BitSet cells) {
        List<Interval<T>> out = new ArrayList<>();
        int j = 0;
        while (j < cellCount) {
            if (!cells.get(j)) {
                j++;
                continue;
            }
            int start = j;
            while (j < cellCount && cells.get(j)) {
                j++;
            }
            int end = j - 1;
            out.add(Interval.of(boundaries[start], boundaries[end + 1]));
        }
        return out;
    }

    static BitSet bitsOf(long mask, int n) {
        BitSet bits = new BitSet(n);
        for (int i = 0; i < n; i++) {
            if ((mask & (1L << i)) != 0) {
                bits.set(i);
            }
        }
        return bits;
    }

    BitSet allCells() {
        BitSet bits = new BitSet(cellCount);
        bits.set(0, cellCount);
        return bits;
    }
}
