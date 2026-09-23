package qsummary;

import java.util.Random;

/**
 * Shared test utilities: fixed-seed data generators and an exact-rank oracle
 * built from a sorted copy of the data. The oracle stores every sample; that
 * exists only in tests to measure the summary against, never in the service.
 */
final class TestData {

    static final long SEED = 20260923L;

    private TestData() {
    }

    /** Exact 1-based rank: number of values <= x (values sorted ascending). */
    static long exactRankLE(double[] sorted, double x) {
        int lo = 0, hi = sorted.length;
        while (lo < hi) {
            int mid = (lo + hi) >>> 1;
            if (sorted[mid] <= x) {
                lo = mid + 1;
            } else {
                hi = mid;
            }
        }
        return lo;
    }

    /** True k-th order statistic (1-based k in [1,n]). */
    static double orderStatistic(double[] sorted, long k) {
        return sorted[(int) k - 1];
    }

    static double[] sorted(double[] data) {
        double[] copy = data.clone();
        java.util.Arrays.sort(copy);
        return copy;
    }

    // ------------------------------------------------------------------
    // Generators (all deterministic: fixed seed)
    // ------------------------------------------------------------------

    static double[] ascending(int n) {
        double[] d = new double[n];
        for (int i = 0; i < n; i++) {
            d[i] = i;
        }
        return d;
    }

    static double[] descending(int n) {
        double[] d = new double[n];
        for (int i = 0; i < n; i++) {
            d[i] = n - i;
        }
        return d;
    }

    static double[] allEqual(int n, double v) {
        double[] d = new double[n];
        java.util.Arrays.fill(d, v);
        return d;
    }

    /** Half zeros, half ones (heavy ties at both ends). */
    static double[] binaryTies(int n) {
        double[] d = new double[n];
        for (int i = n / 2; i < n; i++) {
            d[i] = 1;
        }
        return d;
    }

    /** Values drawn uniformly at random from a small alphabet. */
    static double[] smallAlphabet(int n, int alphabet, long seed) {
        Random r = new Random(seed);
        double[] d = new double[n];
        for (int i = 0; i < n; i++) {
            d[i] = r.nextInt(alphabet);
        }
        return d;
    }

    /** Uniform(0,1) i.i.d. values. */
    static double[] uniform(int n, long seed) {
        Random r = new Random(seed);
        double[] d = new double[n];
        for (int i = 0; i < n; i++) {
            d[i] = r.nextDouble();
        }
        return d;
    }

    /**
     * Heavy-tailed Pareto(shape 1.1) values: a few dominate, many tiny.
     * Quantiles near the top are the hardest case.
     */
    static double[] pareto(int n, double shape, long seed) {
        Random r = new Random(seed);
        double[] d = new double[n];
        for (int i = 0; i < n; i++) {
            double u = r.nextDouble();
            d[i] = 1d / Math.pow(1d - u, 1d / shape);
        }
        return d;
    }

    /** lognormal values (another positive-skew heavy-tail family). */
    static double[] lognormal(int n, long seed) {
        Random r = new Random(seed);
        double[] d = new double[n];
        for (int i = 0; i < n; i++) {
            d[i] = Math.exp(r.nextGaussian() * 2.0);
        }
        return d;
    }

    /** Interleaved min/max adversarial ordering. */
    static double[] adversarial(int n) {
        double[] d = new double[n];
        int lo = 0, hi = n - 1;
        for (int i = 0; i < n; i++) {
            d[i] = (i % 2 == 0) ? lo++ : hi--;
        }
        return d;
    }
}
