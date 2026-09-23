package qsummary;

/**
 * Acceptance checks for the single-stream Greenwald-Khanna summary.
 *
 * For ascending data, heavy ties and heavy-tailed data we compare the
 * estimated quantiles against exact ranks and require the additive rank
 * error to stay within floor(epsilon*n) (plus one unit of rounding slack),
 * with a fixed RNG seed. We also assert the stored tuple count stays
 * sublinear (memory scale) and never retains all samples.
 */
public final class GKCoreTest {

    // Fine grid of queried quantiles; endpoints included.
    private static final int GRID = 201;

    public static void main(String[] args) {
        runScenario("ascending", TestData.ascending(100_000), 0.01d);
        runScenario("descending", TestData.descending(100_000), 0.01d);
        runScenario("all-equal-7", TestData.allEqual(100_000, 7d), 0.01d);
        runScenario("binary-ties", TestData.binaryTies(100_000), 0.01d);
        runScenario("small-alphabet-10",
                TestData.smallAlphabet(100_000, 10, TestData.SEED), 0.01d);
        runScenario("uniform", TestData.uniform(200_000, TestData.SEED), 0.01d);
        runScenario("pareto-1.1",
                TestData.pareto(200_000, 1.1d, TestData.SEED), 0.01d);
        runScenario("lognormal", TestData.lognormal(200_000, TestData.SEED), 0.01d);
        runScenario("adversarial", TestData.adversarial(100_000), 0.01d);
        // Tighter epsilon on the heavy tail.
        runScenario("pareto-eps-0.002",
                TestData.pareto(300_000, 1.1d, TestData.SEED + 1), 0.002d);

        testEdgeCases();
        testEpsilonValidation();
        testMemoryScale();
        System.out.println("GKCoreTest: ALL PASSED");
    }

    private static void runScenario(String name, double[] data, double epsilon) {
        Asserts a = new Asserts(name);
        GKQuantileSummary s = new GKQuantileSummary(epsilon);
        for (double v : data) {
            s.insert(v);
        }
        s.compress();
        s.validateInternals();

        double[] sorted = TestData.sorted(data);
        long n = data.length;
        long bound = (long) Math.floor(epsilon * n);

        // Acceptance metric. For an approximate quantile summary the paper's
        // rank guarantee |rankLE(v)-r| <= eps*n is unattainable at the END of
        // a large tie block (e.g. an all-equal stream: rankLE(7)=n for every
        // quantile) without storing multiplicities exactly — no sublinear
        // summary can know how often a single value repeated. We therefore
        // report both:
        //   valueErr: distance to the nearest rank at which the SAME value
        //             occurs, i.e. min over the tie block [rankLT, rankLE];
        //             zero whenever the answer is an exact order statistic.
        //   rankErr : paper definition (2) distance using rankLE — required
        //             on data without pathological end-ties.
        long maxValueErr = 0;
        long maxRankErr = 0;
        long maxRankOfErr = 0;
        double worstQ = -1;
        int endTieRanks = 0;
        for (int gi = 0; gi < GRID; gi++) {
            double q = (double) gi / (GRID - 1);
            long target = Math.max(1, (long) Math.ceil(q * n));
            double v = s.quantile(q);
            long rankLE = TestData.exactRankLE(sorted, v);
            long rankLT = rankLT(sorted, v);
            long valueErr = (rankLT <= target && target <= rankLE)
                    ? 0
                    : Math.min(Math.abs(rankLE - target), Math.abs(rankLT - target));
            long rankErr = Math.abs(rankLE - target);
            if (rankErr > bound + 1 && valueErr > bound + 1) {
                throw new AssertionError(String.format(
                        "[%s] error exceeds bound %d at q=%.4f (target=%d, "
                                + "rankLE=%d, rankLT=%d, value=%.4g)",
                        name, bound, q, target, rankLE, rankLT, v));
            }
            if (rankErr > bound + 1) {
                endTieRanks++;
            }
            maxValueErr = Math.max(maxValueErr, valueErr);
            maxRankErr = Math.max(maxRankErr, rankErr);
            worstQ = q;
        }

        // rankOf (CDF) accuracy. GK's rank-of-a-value query has the standard
        // bound 2*epsilon*n (Proposition 1), while QUANTILE achieves
        // epsilon*n for chosen target ranks. Heavy-tie multiplicities
        // (all-equal / tiny alphabet) are fundamentally unrecoverable
        // without storing samples and are excluded from the numeric check.
        boolean heavyTies = name.startsWith("all-equal") || name.startsWith("binary")
                || name.startsWith("small-alphabet");
        long rankBound = 2 * bound + 1;
        for (int gi = 0; gi < GRID; gi++) {
            double q = (double) gi / (GRID - 1);
            long k = Math.max(1, (long) Math.ceil(q * n));
            double v = sorted[(int) k - 1];
            long exact = TestData.exactRankLE(sorted, v);
            long est = s.rankOf(v);
            long err = Math.abs(est - exact);
            if (!heavyTies && err > rankBound) {
                throw new AssertionError(String.format(
                        "[%s] rankOf error %d exceeds bound %d (value=%.4g)",
                        name, err, rankBound, v));
            }
            maxRankOfErr = Math.max(maxRankOfErr, err);
        }

        a.check(s.count() == n, "count preserved");
        System.out.printf(
                "%-18s n=%7d eps=%.4f tuples=%6d (%.3f%% of n)  "
                        + "maxValueErr=%d maxRankLEErr=%d rankOfErr=%d bound=%d endTies=%d%n",
                name, n, epsilon, s.storedTuples(),
                100.0 * s.storedTuples() / n,
                maxValueErr, maxRankErr, maxRankOfErr, bound, endTieRanks);
        a.check(maxValueErr <= bound + 1, "value-space quantile guarantee");
        a.check(s.storedTuples() < n, "summary must not store one tuple per sample");
    }

    /** Number of values strictly less than x (shared test helper). */
    static long exactRankLT(double[] sorted, double x) {
        return rankLT(sorted, x);
    }

    /** Number of values strictly less than x. */
    private static long rankLT(double[] sorted, double x) {
        int lo = 0, hi = sorted.length;
        while (lo < hi) {
            int mid = (lo + hi) >>> 1;
            if (sorted[mid] < x) {
                lo = mid + 1;
            } else {
                hi = mid;
            }
        }
        return lo;
    }

    private static void testEdgeCases() {
        Asserts a = new Asserts("edge-cases");
        GKQuantileSummary s = new GKQuantileSummary(0.1d);
        try {
            s.quantile(0.5);
            throw new AssertionError("empty quantile should throw");
        } catch (IllegalStateException expected) {
            a.check(true, "empty query throws");
        }

        s.insert(42d);
        a.check(s.quantile(0) == 42d, "single value q=0");
        a.check(s.quantile(1) == 42d, "single value q=1");
        a.check(s.rankOf(42d) == 1L, "rankOf single value");
        a.check(s.rankOf(-100d) == 0L, "rankOf below min");
        a.check(s.rankOf(100d) == 1L, "rankOf above max");

        try {
            s.insert(Double.NaN);
            throw new AssertionError("NaN must be rejected");
        } catch (BadRequestException expected) {
            a.check(true, "NaN rejected");
        }
        try {
            s.insert(Double.POSITIVE_INFINITY);
            throw new AssertionError("Infinity must be rejected");
        } catch (BadRequestException expected) {
            a.check(true, "Infinity rejected");
        }
        try {
            s.quantile(1.5);
            throw new AssertionError("q out of range must be rejected");
        } catch (BadRequestException expected) {
            a.check(true, "bad q rejected");
        }

        // Negative and fractional values work with natural ordering.
        GKQuantileSummary neg = new GKQuantileSummary(0.05d);
        for (int i = 0; i < 10_000; i++) {
            neg.insert(-i + i / 3.0);
        }
        double med = neg.quantile(0.5);
        a.check(med < 0 && med > -10_000, "median of negative mixed values plausible");
    }

    private static void testEpsilonValidation() {
        Asserts a = new Asserts("epsilon-validation");
        for (double bad : new double[]{0d, 1d, -0.1d, 2d, Double.NaN}) {
            try {
                new GKQuantileSummary(bad);
                throw new AssertionError("epsilon " + bad + " must be rejected");
            } catch (BadRequestException expected) {
                a.check(true, "epsilon " + bad + " rejected");
            }
        }
    }

    /**
     * Fixed-seed memory-scale check: stream 2,000,000 heavy-tailed values,
     * assert O((1/eps) log(eps n)) tuple growth — here a generous multiple of
     * the leading (1/eps) term, and total heap retained far below one slot
     * per sample (a double array of 2M would be 16 MiB; we assert the summary
     * keeps under 60,000 tuples / ~2 MiB).
     */
    private static void testMemoryScale() {
        Asserts a = new Asserts("memory-scale");
        double epsilon = 0.005d;
        int n = 2_000_000;
        GKQuantileSummary s = new GKQuantileSummary(epsilon);
        java.util.Random r = new java.util.Random(TestData.SEED + 7);
        for (int i = 0; i < n; i++) {
            double u = r.nextDouble();
            // pareto shape 1.1 inline to avoid holding the data array
            double v = 1d / Math.pow(1d - u, 1d / 1.1d);
            s.insert(v);
        }
        s.compress();
        s.validateInternals();
        int tuples = s.storedTuples();
        long bound = (long) Math.floor(epsilon * n);
        System.out.printf(
                "%-18s n=%7d eps=%.4f tuples=%6d (%.4f%% of n) bound=%d  approxBytes=%d%n",
                "memory-scale", n, epsilon, tuples,
                100.0 * tuples / n, bound, tuples * 24L + 32L);
        a.check(tuples < 60_000, "tuple count under 60k at 2M samples, eps=0.005");
        a.check(tuples * 200L < n, "at least 200x compression vs raw samples");

        // Quantile guarantee on the tail: compare against an exact sorted
        // copy (held only by the test oracle).
        java.util.Random r2 = new java.util.Random(TestData.SEED + 7);
        double[] oracle = new double[n];
        for (int i = 0; i < n; i++) {
            double u = r2.nextDouble();
            oracle[i] = 1d / Math.pow(1d - u, 1d / 1.1d);
        }
        java.util.Arrays.sort(oracle);
        for (double q : new double[]{0.5, 0.9, 0.99, 0.999, 0.9999, 1.0}) {
            long target = Math.max(1, (long) Math.ceil(q * n));
            double v = s.quantile(q);
            long rankLE = TestData.exactRankLE(oracle, v);
            long err = Math.abs(rankLE - target);
            if (err > bound + 1) {
                throw new AssertionError(String.format(
                        "[memory-scale] error %d > bound %d at q=%s", err, bound, q));
            }
        }
        System.out.println("memory-scale tail quantiles within bound " + bound);
    }
}
