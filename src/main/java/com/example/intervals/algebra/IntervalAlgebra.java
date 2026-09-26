package com.example.intervals.algebra;

import com.example.intervals.model.Cut;
import com.example.intervals.model.Interval;
import com.example.intervals.model.IntervalSet;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.function.Predicate;

/**
 * Set algebra over {@link IntervalSet}s.
 *
 * <p>Every operation routes through one cut-based sweep line. All lower and
 * upper cuts of every operand interval are collected into a single sorted,
 * de-duplicated list that always includes {@code -∞} and {@code +∞}. Each pair
 * of consecutive cuts {@code (c, d)} is an atomic region containing either a
 * single point (when {@code c=BELOW(v)} and {@code d=ABOVE(v)}) or an open
 * chunk of the line; an operand covers it iff some interval of that operand
 * has {@code lower <= c && upper > c}. Boolean predicates over per-operand
 * coverage then yield union / intersection / difference / complement.
 *
 * <p>Adjacent covered regions are merged automatically (so {@code [1,2]} and
 * {@code (2,3)} normalize to {@code [1,3)}), singleton regions survive as
 * {@code [v,v]}, and unbounded runs emit infinite cuts. The output is the
 * unique normalized representation, sorted by lower cut.
 */
public final class IntervalAlgebra<T extends Comparable<? super T>> {

    /** Normalizes a single collection of intervals into a disjoint sorted union. */
    public IntervalSet<T> normalize(List<Interval<T>> intervals) {
        List<List<Interval<T>>> groups = new ArrayList<>();
        groups.add(new ArrayList<>(intervals));
        return sweep(groups, cover -> cover[0]);
    }

    public IntervalSet<T> union(IntervalSet<T> a, IntervalSet<T> b) {
        return sweep(List.of(a.intervals(), b.intervals()), cover -> cover[0] || cover[1]);
    }

    public IntervalSet<T> intersection(IntervalSet<T> a, IntervalSet<T> b) {
        return sweep(List.of(a.intervals(), b.intervals()), cover -> cover[0] && cover[1]);
    }

    /** Set difference {@code a - b} (relative complement inside a). */
    public IntervalSet<T> difference(IntervalSet<T> a, IntervalSet<T> b) {
        return sweep(List.of(a.intervals(), b.intervals()), cover -> cover[0] && !cover[1]);
    }

    /** Absolute complement relative to the whole domain line: everything not in a. */
    public IntervalSet<T> complement(IntervalSet<T> a) {
        return sweep(List.of(a.intervals()), cover -> !cover[0]);
    }

    /**
     * Generic cut sweep.
     *
     * @param groups    one list of intervals per operand
     * @param predicate decides whether an atomic region belongs to the result
     */
    private IntervalSet<T> sweep(List<List<Interval<T>>> groups, Predicate<boolean[]> predicate) {
        int n = groups.size();

        // cut -> per-group delta applied when scanning FROM that cut rightwards.
        // Starts (+1) are registered at the lower cut; ends (-1) at the upper cut.
        Map<Cut<T>, int[]> deltas = new LinkedHashMap<>();
        deltas.put(Cut.negInfinity(), new int[n]);
        deltas.put(Cut.posInfinity(), new int[n]);

        for (int g = 0; g < n; g++) {
            for (Interval<T> iv : groups.get(g)) {
                // Degenerate forms such as (x,x) or [x,x) never cover an atomic
                // region and are simply skipped during the sweep.
                if (iv.isEmpty()) {
                    continue;
                }
                deltas.computeIfAbsent(iv.lowerCut(), k -> new int[n])[g]++;
                deltas.computeIfAbsent(iv.upperCut(), k -> new int[n])[g]--;
            }
        }

        List<Cut<T>> cuts = new ArrayList<>(deltas.keySet());
        cuts.sort(Comparator.naturalOrder());

        int[] active = new int[n];
        List<Interval<T>> result = new ArrayList<>();
        Cut<T> runStart = null;

        for (int i = 0; i + 1 < cuts.size(); i++) {
            Cut<T> c = cuts.get(i);
            int[] delta = deltas.get(c);
            for (int g = 0; g < n; g++) {
                active[g] += delta[g];
            }

            boolean[] cover = new boolean[n];
            for (int g = 0; g < n; g++) {
                cover[g] = active[g] > 0;
            }
            boolean inResult = predicate.test(cover);

            if (inResult && runStart == null) {
                runStart = c;
            } else if (!inResult && runStart != null) {
                result.add(Interval.of(runStart, c));
                runStart = null;
            }
        }
        if (runStart != null) {
            // The run extends to the final cut, which is +infinity.
            result.add(Interval.of(runStart, Cut.posInfinity()));
        }
        return new IntervalSet<>(result);
    }
}
