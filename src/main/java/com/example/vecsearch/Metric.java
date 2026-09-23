package com.example.vecsearch;

/**
 * Distance metrics supported by the service.
 *
 * L2: squared Euclidean distance (monotonic with Euclidean distance,
 *     so ranking and k-NN results are identical; the squared form
 *     avoids a sqrt per pair).
 *
 * COSINE: 1 - cosine similarity = 1 - (a.b / (|a| * |b|)).
 *     Undefined for zero vectors; insert and search reject them.
 */
public enum Metric {
    L2,
    COSINE;

    public static Metric fromString(String raw) {
        if (raw == null) {
            throw new ApiException(400, "missing field: metric (must be \"L2\" or \"COSINE\")");
        }
        try {
            return Metric.valueOf(raw.toUpperCase());
        } catch (IllegalArgumentException e) {
            throw new ApiException(400, "unknown metric \"" + raw + "\" (must be \"L2\" or \"COSINE\")");
        }
    }
}
