package com.example.neardup;

/**
 * Immutable pipeline configuration. All defaults are fixed constants so
 * results are reproducible: same input + same version = same clusters.
 *
 * @param shingleSize word-shingle size k (tokens per shingle)
 * @param numHashes   MinHash signature length
 * @param bands       LSH band count; numHashes must be a multiple of bands
 * @param threshold   exact-Jaccard acceptance threshold, in (0, 1]
 * @param seed        fixed seed for the base hash
 */
public record NearDupConfig(int shingleSize, int numHashes, int bands, double threshold, long seed) {

    public static final int DEFAULT_SHINGLE_SIZE = 3;
    public static final int DEFAULT_NUM_HASHES = 256;
    public static final int DEFAULT_BANDS = 64;
    public static final double DEFAULT_THRESHOLD = 0.5;
    public static final long DEFAULT_SEED = 42L;

    public NearDupConfig {
        if (shingleSize < 1) {
            throw new IllegalArgumentException("shingleSize must be >= 1");
        }
        if (numHashes < 1 || numHashes % bands != 0) {
            throw new IllegalArgumentException("numHashes must be a positive multiple of bands");
        }
        if (!(threshold > 0.0 && threshold <= 1.0)) {
            throw new IllegalArgumentException("threshold must be in (0, 1], got " + threshold);
        }
    }

    public static NearDupConfig defaults() {
        return new NearDupConfig(DEFAULT_SHINGLE_SIZE, DEFAULT_NUM_HASHES, DEFAULT_BANDS,
                DEFAULT_THRESHOLD, DEFAULT_SEED);
    }

    public NearDupConfig withThreshold(double newThreshold) {
        return new NearDupConfig(shingleSize, numHashes, bands, newThreshold, seed);
    }
}
