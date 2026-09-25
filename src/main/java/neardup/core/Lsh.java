package neardup.core;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * Banding LSH over MinHash signatures — the candidate-recall stage.
 *
 * Fixed configuration matching {@link MinHash#DEFAULT_NUM_HASHES}:
 *   numHashes = 120, bands = 60, rows = 2 (bands*rows = 120).
 *
 * A pair becomes a candidate if ANY band's {@code rows} hash values are equal
 * between the two signatures. The single-band collision probability for true
 * Jaccard s is s^rows = s^2, so the all-band S-curve is 1 - (1 - s^2)^60:
 *   s = 0.2 -> ~0.92     s = 0.33 -> ~0.999     s = 0.6 -> ~1.0
 * This deliberately errs toward high recall: the band threshold (1-0.5)^(1/2)
 * is low, so false-positive candidates are EXPECTED; exact Jaccard verification
 * (Clusterer) removes them. Both rates are measured and reported.
 *
 * Empty-shingle documents (sentinel signatures) are never indexed.
 */
public final class Lsh {

    public static final int DEFAULT_BANDS = 60;
    public static final int DEFAULT_ROWS = 2;

    private final int bands;
    private final int rows;

    public Lsh() {
        this(DEFAULT_BANDS, DEFAULT_ROWS);
    }

    public Lsh(int bands, int rows) {
        if (bands < 1 || rows < 1) {
            throw new IllegalArgumentException("bands and rows must be >= 1");
        }
        this.bands = bands;
        this.rows = rows;
    }

    public int bands() {
        return bands;
    }

    public int rows() {
        return rows;
    }

    /** A symmetric, deduplicated candidate pair (candidate recall output). */
    public record CandidatePair(int i, int j, int bandHits) implements Comparable<CandidatePair> {
        @Override
        public int compareTo(CandidatePair o) {
            int c = Integer.compare(i, o.i);
            return c != 0 ? c : Integer.compare(j, o.j);
        }
    }

    /**
     * @param signatures signatures in document-index order
     * @return deduplicated candidate pairs with the number of colliding bands
     */
    public List<CandidatePair> candidates(long[][] signatures) {
        int needed = bands * rows;
        for (long[] s : signatures) {
            if (s.length < needed) {
                throw new IllegalArgumentException(
                        "signature length " + s.length + " < bands*rows " + needed);
            }
        }

        // key = (band, rows-tuple) -> bucket of document indices
        Map<BandKey, List<Integer>> buckets = new HashMap<>();
        for (int doc = 0; doc < signatures.length; doc++) {
            long[] sig = signatures[doc];
            if (isEmpty(sig)) {
                continue;
            }
            for (int band = 0; band < bands; band++) {
                long[] tuple = new long[rows];
                System.arraycopy(sig, band * rows, tuple, 0, rows);
                buckets.computeIfAbsent(new BandKey(band, tuple), k -> new ArrayList<>()).add(doc);
            }
        }

        Map<Long, int[]> hits = new HashMap<>(); // packed pair -> [bandHits]
        for (List<Integer> bucket : buckets.values()) {
            for (int a = 0; a < bucket.size(); a++) {
                for (int b = a + 1; b < bucket.size(); b++) {
                    int x = bucket.get(a);
                    int y = bucket.get(b);
                    if (x > y) {
                        int t = x;
                        x = y;
                        y = t;
                    }
                    long key = ((long) x << 32) | (y & 0xffffffffL);
                    int[] h = hits.computeIfAbsent(key, k -> new int[1]);
                    h[0]++;
                }
            }
        }

        List<CandidatePair> out = new ArrayList<>(hits.size());
        for (Map.Entry<Long, int[]> e : hits.entrySet()) {
            int x = (int) (e.getKey() >>> 32);
            int y = (int) (e.getKey() & 0xffffffffL);
            out.add(new CandidatePair(x, y, e.getValue()[0]));
        }
        out.sort(null);
        return out;
    }

    private static boolean isEmpty(long[] sig) {
        for (long v : sig) {
            if (v != Long.MAX_VALUE) {
                return false;
            }
        }
        return true;
    }

    private record BandKey(int band, long[] tuple) {
        @Override
        public boolean equals(Object o) {
            if (!(o instanceof BandKey other)) {
                return false;
            }
            return band == other.band && Arrays.equals(tuple, other.tuple);
        }

        @Override
        public int hashCode() {
            return 31 * band + Arrays.hashCode(tuple);
        }
    }
}
