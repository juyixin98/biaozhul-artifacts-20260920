package joinplanner.sim;

import java.util.Random;

/**
 * Generates synthetic rows for join columns.
 *
 * <p>A column with ndv {@code N} and Zipf exponent {@code z} emits values in
 * {@code [0,N)} where value {@code k} has probability proportional to
 * {@code 1/(k+1)^z}. {@code z=0} is uniform. Weighted sampling is done with the
 * alias-free cumulative-table approach (fine for the small NDVs used here).
 */
public final class DataGenerator {

    private DataGenerator() {
    }

    /** Cumulative weights [0..N]; cum[N] = total weight. */
    public static double[] cumulativeWeights(long ndv, double zipf) {
        double[] cum = new double[(int) ndv + 1];
        for (int k = 0; k < ndv; k++) {
            cum[k + 1] = cum[k] + 1.0 / Math.pow(k + 1.0, zipf);
        }
        return cum;
    }

    /** One sample value in [0, ndv). */
    public static int sample(double[] cum, Random rng) {
        double target = rng.nextDouble() * cum[cum.length - 1];
        int lo = 0;
        int hi = cum.length - 1;
        while (lo < hi - 1) {
            int mid = (lo + hi) >>> 1;
            if (cum[mid] <= target) {
                lo = mid;
            } else {
                hi = mid;
            }
        }
        return lo;
    }
}
