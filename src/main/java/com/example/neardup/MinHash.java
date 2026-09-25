package com.example.neardup;

import java.util.Set;

/**
 * MinHash signatures with a fixed seed.
 *
 * <p>Each shingle is hashed once with MurmurHash3 x64 128-bit (fixed seed
 * from {@link NearDupConfig#seed()}), yielding two 64-bit halves h1, h2.
 * The i-th permutation hash uses double hashing: h_i(x) = h1 + i * h2
 * (mod 2^64 via long overflow). The signature is the per-i minimum over
 * all shingles. The fraction of equal positions between two signatures is
 * an unbiased estimator of the Jaccard similarity of the shingle sets.
 *
 * <p>An empty shingle set yields a signature of all {@code Long.MAX_VALUE}.
 */
public final class MinHash {

    private final int numHashes;
    private final long seed;

    public MinHash(int numHashes, long seed) {
        if (numHashes < 1) {
            throw new IllegalArgumentException("numHashes must be >= 1, got " + numHashes);
        }
        this.numHashes = numHashes;
        this.seed = seed;
    }

    public long[] signature(Set<String> shingles) {
        long[] sig = new long[numHashes];
        java.util.Arrays.fill(sig, Long.MAX_VALUE);
        for (String shingle : shingles) {
            long[] h = MurmurHash3.hash128(shingle, seed);
            long h1 = h[0];
            long h2 = h[1];
            for (int i = 0; i < numHashes; i++) {
                long value = h1 + i * h2;
                if (value < sig[i]) {
                    sig[i] = value;
                }
            }
        }
        return sig;
    }

    /** Fraction of equal positions — estimates Jaccard similarity. */
    public static double estimatedSimilarity(long[] a, long[] b) {
        if (a.length != b.length) {
            throw new IllegalArgumentException("signature length mismatch");
        }
        int equal = 0;
        for (int i = 0; i < a.length; i++) {
            if (a[i] == b[i]) {
                equal++;
            }
        }
        return (double) equal / a.length;
    }
}
