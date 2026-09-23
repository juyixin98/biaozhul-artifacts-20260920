package joinplanner.core;

/**
 * Floating-point helpers for the two regimes the planner works in:
 *
 * <ul>
 *   <li><b>cardinalities</b> are multiplied (up to 8 tables) — products can overflow
 *       to Infinity, so internally we track log(cardinality) and translate back
 *       carefully (e.g. 0 rows &harr; -infinity).</li>
 *   <li><b>costs</b> are sums of intermediate cardinalities — added in log domain
 *       via logAdd so inputs like 1e300 rows never collapse into Infinity + x.</li>
 * </ul>
 */
public final class Stats {

    /** Rows above this are reported with the "overflow" flag set. */
    public static final double OVERFLOW_THRESHOLD = 1e300;

    private Stats() {
    }

    public static double logRows(double rows) {
        if (rows < 0) {
            throw new IllegalArgumentException("negative row count: " + rows);
        }
        return rows == 0.0 ? Double.NEGATIVE_INFINITY : Math.log(rows);
    }

    /** exp() of a log-cardinality, returning 0.0 for -infinity and Infinity for +infinity. */
    public static double fromLog(double logRows) {
        if (logRows == Double.NEGATIVE_INFINITY) {
            return 0.0;
        }
        if (logRows == Double.POSITIVE_INFINITY || logRows > Math.log(Double.MAX_VALUE)) {
            return Double.POSITIVE_INFINITY;
        }
        return Math.exp(logRows);
    }

    /**
     * Numerically stable log(a + b) given log(a) and log(b), with -infinity
     * meaning "zero rows". Handles infinite-information combinations.
     */
    public static double logAdd(double logA, double logB) {
        if (logA == Double.NEGATIVE_INFINITY) {
            return logB;
        }
        if (logB == Double.NEGATIVE_INFINITY) {
            return logA;
        }
        if (logA == Double.POSITIVE_INFINITY || logB == Double.POSITIVE_INFINITY) {
            return Double.POSITIVE_INFINITY;
        }
        double max = Math.max(logA, logB);
        if (max <= Math.log(Double.MAX_VALUE) - 1.0 && max > -745.0) {
            // exp differences are safe
            return max + Math.log1p(Math.exp(-Math.abs(logA - logB)));
        }
        // Near representability limits: fall back to raw addition in linear space
        double s = fromLog(logA) + fromLog(logB);
        return s == Double.POSITIVE_INFINITY ? Double.POSITIVE_INFINITY : Math.log(s);
    }

    public static boolean overflowed(double rows) {
        return Double.isInfinite(rows) || rows > OVERFLOW_THRESHOLD;
    }
}
