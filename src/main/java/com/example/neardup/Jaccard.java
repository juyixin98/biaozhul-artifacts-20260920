package com.example.neardup;

import java.util.Set;

/** Exact Jaccard similarity on shingle sets. */
public final class Jaccard {

    private Jaccard() {
    }

    /**
     * Returns |A ∩ B| / |A ∪ B|.
     *
     * <p>Convention: if either set is empty the result is 0.0. Two empty
     * shingle sets (e.g. two identical two-word documents) are NOT treated
     * as similar — with no shingles there is no evidence to compare, and
     * treating them as identical would collapse all short documents into
     * one cluster.
     */
    public static double similarity(Set<String> a, Set<String> b) {
        if (a.isEmpty() || b.isEmpty()) {
            return 0.0;
        }
        Set<String> smaller = a.size() <= b.size() ? a : b;
        Set<String> larger = smaller == a ? b : a;
        long intersection = smaller.stream().filter(larger::contains).count();
        long union = (long) a.size() + b.size() - intersection;
        return (double) intersection / (double) union;
    }
}
