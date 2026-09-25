package neardup.core;

import java.math.BigInteger;
import java.util.Random;
import java.util.Set;

/**
 * MinHash signatures with FIXED, seeded permutations.
 *
 * Each of the {@code numHashes} permutations is the affine family
 *   h_i(x) = (a_i * x + b_i) mod p
 * with p = 2^61 - 1 (a Mersenne prime), a_i in [1,p), b_i in [0,p), drawn from
 * a {@link Random} seeded with {@link #SEED}. The seed is a hard part of the
 * algorithm contract: changing it changes which pairs are recalled, so it is
 * fixed and reported (never derived from the input).
 *
 * Shingle ids are FNV-1a 64-bit values reduced into [0,p) with a second
 * reduction fold (because the raw 64-bit value can exceed p).
 */
public final class MinHash {

    /** Fixed seed for permutation generation. */
    public static final long SEED = 20260924L;

    public static final int DEFAULT_NUM_HASHES = 120;

    /** Mersenne prime 2^61 - 1. */
    static final long P = (1L << 61) - 1;

    private final int numHashes;
    private final long[] a;
    private final long[] b;

    public MinHash() {
        this(DEFAULT_NUM_HASHES, SEED);
    }

    public MinHash(int numHashes, long seed) {
        if (numHashes < 1) {
            throw new IllegalArgumentException("numHashes must be >= 1");
        }
        this.numHashes = numHashes;
        this.a = new long[numHashes];
        this.b = new long[numHashes];
        Random rnd = new Random(seed);
        for (int i = 0; i < numHashes; i++) {
            long ai = 1L + (rnd.nextLong() & Long.MAX_VALUE) % (P - 1);
            long bi = rnd.nextLong() & Long.MAX_VALUE;
            this.a[i] = ai;
            this.b[i] = bi;
        }
    }

    public int numHashes() {
        return numHashes;
    }

    public long seed() {
        return SEED;
    }

    /** Compute the MinHash signature of a shingle set. */
    public long[] signature(Set<Long> shingles) {
        long[] sig = new long[numHashes];
        for (int i = 0; i < numHashes; i++) {
            sig[i] = Long.MAX_VALUE;
        }
        if (shingles.isEmpty()) {
            // Empty document: signature stays at +inf sentinel; LSH puts these
            // nowhere (see Lsh), and exact Jaccard also rejects empty matches.
            return sig;
        }
        for (long shingle : shingles) {
            long x = reduce(shingle);
            for (int i = 0; i < numHashes; i++) {
                long h = hashOne(a[i], b[i], x);
                if (h < sig[i]) {
                    sig[i] = h;
                }
            }
        }
        return sig;
    }

    /** Fraction of signature positions that agree — the MinHash estimator of Jaccard. */
    public static double estimatedSimilarity(long[] s1, long[] s2) {
        if (s1.length != s2.length) {
            throw new IllegalArgumentException("signature length mismatch");
        }
        // Two empty documents carry the all-sentinel signature; never let their
        // trivial equality read as similarity 1.
        if (isAllSentinel(s1) || isAllSentinel(s2)) {
            return 0.0;
        }
        int eq = 0;
        for (int i = 0; i < s1.length; i++) {
            if (s1[i] == s2[i]) {
                eq++;
            }
        }
        return (double) eq / (double) s1.length;
    }

    private static boolean isAllSentinel(long[] sig) {
        for (long v : sig) {
            if (v != Long.MAX_VALUE) {
                return false;
            }
        }
        return true;
    }

    private static long reduce(long id) {
        long v = id & Long.MAX_VALUE; // [0, 2^63)
        if (v >= P) {
            v = v % P;
        }
        return v;
    }

    private static long hashOne(long a, long b, long x) {
        // (a*x + b) mod p without 64-bit overflow, via BigInteger.
        BigInteger aa = BigInteger.valueOf(a);
        BigInteger xx = BigInteger.valueOf(x);
        BigInteger bb = BigInteger.valueOf(b);
        BigInteger pp = BigInteger.valueOf(P);
        return aa.multiply(xx).add(bb).mod(pp).longValue();
    }
}
