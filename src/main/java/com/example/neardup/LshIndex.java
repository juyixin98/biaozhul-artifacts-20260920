package com.example.neardup;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * LSH banding index over MinHash signatures. The signature is split into
 * {@code bands} bands of {@code rows} rows each; two documents become
 * candidates iff they agree on every row of at least one band. Candidate
 * generation is approximate — every candidate must be re-checked with
 * exact Jaccard before it is accepted.
 */
public final class LshIndex {

    /** An unordered pair of document indexes (into the input list). */
    public record DocPair(int a, int b) {
        public DocPair {
            if (a > b) {
                int t = a;
                a = b;
                b = t;
            }
        }
    }

    private final int bands;
    private final int rows;

    public LshIndex(int numHashes, int bands) {
        if (bands < 1 || numHashes % bands != 0) {
            throw new IllegalArgumentException(
                    "numHashes (" + numHashes + ") must be a positive multiple of bands (" + bands + ")");
        }
        this.bands = bands;
        this.rows = numHashes / bands;
    }

    public int rowsPerBand() {
        return rows;
    }

    /** Returns all candidate pairs sharing at least one band bucket. */
    public Set<DocPair> candidatePairs(List<long[]> signatures) {
        Map<BandKey, List<Integer>> buckets = new HashMap<>();
        for (int doc = 0; doc < signatures.size(); doc++) {
            long[] sig = signatures.get(doc);
            for (int band = 0; band < bands; band++) {
                long[] slice = Arrays.copyOfRange(sig, band * rows, (band + 1) * rows);
                buckets.computeIfAbsent(new BandKey(band, slice), k -> new ArrayList<>()).add(doc);
            }
        }
        Set<DocPair> pairs = new LinkedHashSet<>();
        for (List<Integer> bucket : buckets.values()) {
            for (int i = 0; i < bucket.size(); i++) {
                for (int j = i + 1; j < bucket.size(); j++) {
                    pairs.add(new DocPair(bucket.get(i), bucket.get(j)));
                }
            }
        }
        return pairs;
    }

    private record BandKey(int band, long[] rows) {
        @Override
        public boolean equals(Object other) {
            return other instanceof BandKey that
                    && band == that.band
                    && Arrays.equals(rows, that.rows);
        }

        @Override
        public int hashCode() {
            return 31 * band + Arrays.hashCode(rows);
        }
    }
}
