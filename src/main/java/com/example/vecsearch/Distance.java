package com.example.vecsearch;

/**
 * Distance computations. Every vector-vector distance call increments
 * the per-search {@link Counter}, so search budget (number of distance
 * calculations) can be reported and enforced exactly.
 *
 * Vector norms for cosine are precomputed on insert and are therefore
 * not counted as distance work.
 */
public final class Distance {

    private Distance() {}

    /** Mutable per-search counter of distance calculations. */
    public static final class Counter {
        private long vectorDistances;
        private long centroidDistances;

        public void countVector() {
            vectorDistances++;
        }

        public void countCentroid() {
            centroidDistances++;
        }

        public long vectorDistances() {
            return vectorDistances;
        }

        public long centroidDistances() {
            return centroidDistances;
        }

        public long total() {
            return vectorDistances + centroidDistances;
        }
    }

    /** Squared Euclidean distance. */
    public static double l2(double[] a, double[] b, Counter counter) {
        double sum = 0.0;
        for (int i = 0; i < a.length; i++) {
            double d = a[i] - b[i];
            sum += d * d;
        }
        if (counter != null) {
            counter.countVector();
        }
        return sum;
    }

    /**
     * Cosine distance = 1 - cosine similarity.
     *
     * @param normA precomputed L2 norm of a
     * @param normB precomputed L2 norm of b
     */
    public static double cosine(double[] a, double[] b,
                                double normA, double normB, Counter counter) {
        double dot = 0.0;
        for (int i = 0; i < a.length; i++) {
            dot += a[i] * b[i];
        }
        if (counter != null) {
            counter.countVector();
        }
        if (normA == 0.0 || normB == 0.0) {
            // Callers validate zero vectors up front; this is a defensive guard.
            throw new ApiException(400, "cosine distance is undefined for zero vectors");
        }
        return 1.0 - dot / (normA * normB);
    }

    public static double norm(double[] v) {
        double sum = 0.0;
        for (double x : v) {
            sum += x * x;
        }
        return Math.sqrt(sum);
    }

    /** Distance between a query and a stored vector under the given metric. */
    public static double between(Metric metric, double[] query, double queryNorm,
                                 double[] stored, double storedNorm, Counter counter) {
        return metric == Metric.L2
                ? l2(query, stored, counter)
                : cosine(query, stored, queryNorm, storedNorm, counter);
    }

    /** Distance used while clustering / probing centroids (counted separately). */
    public static double centroidL2(double[] a, double[] b, Counter counter) {
        double d = l2(a, b, null);
        if (counter != null) {
            counter.countCentroid();
        }
        return d;
    }
}
