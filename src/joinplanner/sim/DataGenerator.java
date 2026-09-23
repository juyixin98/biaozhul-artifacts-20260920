package joinplanner.sim;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * Deterministic value generator for a join column.
 *
 * <p>Distributions:
 * <ul>
 *   <li>{@code uniform}  — values 1..ndv, equally likely.</li>
 *   <li>{@code zipf}     — value k gets weight 1/k^s over 1..ndv (s = skew, s≈1 typical).</li>
 *   <li>{@code hotkey}   — one value (1) gets {@code hotFraction} of the rows, the rest
 *       spread uniformly over the other ndv-1 values.</li>
 *   <li>{@code sequence} — values 1..rows (unique column, like a primary key).</li>
 *   <li>{@code frequencies} — explicit per-value counts, one entry per distinct value.</li>
 * </ul>
 */
public final class DataGenerator {

    private DataGenerator() {}

    public static long[] generate(ColumnSpec col, long rows, java.util.Random rng) {
        long[] values = new long[(int) Math.min(rows, Integer.MAX_VALUE)];
        switch (col.distribution) {
            case "uniform" -> fillUniform(values, ndv(col, rows), rng);
            case "zipf" -> fillZipf(values, ndv(col, rows), col.skew, rng);
            case "hotkey" -> fillHotkey(values, ndv(col, rows), col.hotFraction, rng);
            case "sequence" -> fillSequence(values);
            case "frequencies" -> fillFrequencies(values, col.frequencies, rng);
            default -> throw new joinplanner.json.BadInputException(
                    "Unknown distribution '" + col.distribution
                            + "' (uniform|zipf|hotkey|sequence|frequencies)");
        }
        if (col.offset != 0) {
            for (int i = 0; i < values.length; i++) {
                values[i] += col.offset;
            }
        }
        return values;
    }

    /** Actual number of distinct values present in generated data. */
    public static long actualNdv(long[] values) {
        java.util.HashSet<Long> seen = new java.util.HashSet<>();
        for (long v : values) {
            seen.add(v);
        }
        return seen.size();
    }

    private static long ndv(ColumnSpec col, long rows) {
        if (col.ndv <= 0) {
            return rows;
        }
        return Math.min(col.ndv, rows);
    }

    private static void fillUniform(long[] out, long ndv, java.util.Random rng) {
        for (int i = 0; i < out.length; i++) {
            out[i] = 1 + (long) (rng.nextDouble() * ndv);
        }
    }

    private static void fillZipf(long[] out, long ndv, double skew, java.util.Random rng) {
        double[] cumulative = zipfTable(ndv, skew);
        for (int i = 0; i < out.length; i++) {
            out[i] = 1L + sampleCdf(cumulative, rng.nextDouble());
        }
    }

    private static void fillHotkey(long[] out, long ndv, double hotFraction, java.util.Random rng) {
        for (int i = 0; i < out.length; i++) {
            if (rng.nextDouble() < hotFraction) {
                out[i] = 1L;
            } else if (ndv <= 1) {
                out[i] = 1L;
            } else {
                out[i] = 2L + (long) (rng.nextDouble() * (ndv - 1));
            }
        }
    }

    private static void fillSequence(long[] out) {
        for (int i = 0; i < out.length; i++) {
            out[i] = i + 1L;
        }
    }

    private static void fillFrequencies(long[] out, long[] frequencies, java.util.Random rng) {
        double[] cdf = new double[frequencies.length];
        double total = 0;
        for (int i = 0; i < frequencies.length; i++) {
            total += frequencies[i];
            cdf[i] = total;
        }
        for (int i = 0; i < out.length; i++) {
            out[i] = 1L + sampleCdf(cdf, rng.nextDouble() * total);
        }
    }

    static double[] zipfTable(long ndv, double skew) {
        double[] cdf = new double[(int) ndv];
        double sum = 0;
        for (long k = 1; k <= ndv; k++) {
            sum += 1.0 / Math.pow(k, skew);
            cdf[(int) (k - 1)] = sum;
        }
        // Normalize so the final entry is 1.
        for (int i = 0; i < cdf.length; i++) {
            cdf[i] /= sum;
        }
        return cdf;
    }

    static int sampleCdf(double[] cdf, double p) {
        int lo = 0;
        int hi = cdf.length - 1;
        while (lo < hi) {
            int mid = (lo + hi) >>> 1;
            if (p <= cdf[mid]) {
                hi = mid;
            } else {
                lo = mid + 1;
            }
        }
        return lo;
    }

    /** Builds value -> [row indices] hash tables for an actual hash join. */
    public static Map<Long, List<Integer>> hashTable(long[] column) {
        Map<Long, List<Integer>> table = new HashMap<>(column.length * 2);
        for (int i = 0; i < column.length; i++) {
            table.computeIfAbsent(column[i], k -> new ArrayList<>()).add(i);
        }
        return table;
    }
}
