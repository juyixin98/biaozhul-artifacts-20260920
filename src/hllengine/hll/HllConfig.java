package hllengine.hll;

import hllengine.api.ApiException;

import java.util.Map;

/**
 * Immutable sketch configuration. Two sketches may be merged only when their
 * configurations are {@link #compatibleWith(HllConfig) equal}: precision, hash
 * algorithm and hash seed must all match. The hash function itself is frozen
 * in this build ({@link MurmurHash3#HASH_ID}); the seed is part of the
 * configuration because changing it permutes the hashing.
 */
public record HllConfig(int precision, int seed, String hashId) {

    /** Default precision: 4096 registers, nominal relative standard error ~1.041/64. */
    public static final int DEFAULT_PRECISION = 12;
    public static final int DEFAULT_SEED = 0;
    public static final int MIN_PRECISION = 4;
    public static final int MAX_PRECISION = 18;

    public HllConfig {
        if (precision < MIN_PRECISION || precision > MAX_PRECISION) {
            throw new ApiException(ApiException.BAD_REQUEST,
                    "precision must be in [" + MIN_PRECISION + ", " + MAX_PRECISION + "], got " + precision);
        }
        if (hashId == null || !MurmurHash3.HASH_ID.equals(hashId)) {
            throw new ApiException(ApiException.UNSUPPORTED,
                    "unsupported hashId \"" + hashId + "\"; this build only supports " + MurmurHash3.HASH_ID);
        }
    }

    public static HllConfig of(int precision, int seed) {
        return new HllConfig(precision, seed, MurmurHash3.HASH_ID);
    }

    public static HllConfig defaults() {
        return of(DEFAULT_PRECISION, DEFAULT_SEED);
    }

    /** Number of registers m = 2^p. */
    public int m() {
        return 1 << precision;
    }

    /**
     * Nominal relative standard error: 1.04 / sqrt(m). This is an a-priori
     * standard deviation of the estimate, not a measured error and not a bound
     * on any single estimate.
     */
    public double relativeStandardError() {
        return 1.04 / Math.sqrt(m());
    }

    /** Linear-counting threshold: when the raw estimate falls below ~2.5 m, use LC. */
    public double smallRangeThreshold() {
        return 2.5 * m();
    }

    public boolean compatibleWith(HllConfig other) {
        return precision == other.precision && seed == other.seed && hashId.equals(other.hashId);
    }

    public String compatibilityDifference(HllConfig other) {
        if (precision != other.precision) {
            return "precision differs: " + precision + " vs " + other.precision;
        }
        if (seed != other.seed) {
            return "seed differs: " + seed + " vs " + other.seed;
        }
        if (!hashId.equals(other.hashId)) {
            return "hashId differs: " + hashId + " vs " + other.hashId;
        }
        return null;
    }

    public Map<String, Object> toMap() {
        Map<String, Object> m = new java.util.LinkedHashMap<>();
        m.put("precision", precision);
        m.put("seed", seed);
        m.put("hashId", hashId);
        m.put("registers", m());
        m.put("nominalRelativeStandardError", relativeStandardError());
        return m;
    }
}
