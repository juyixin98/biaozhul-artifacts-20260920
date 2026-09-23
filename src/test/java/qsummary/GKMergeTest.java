package qsummary;

import java.util.List;
import java.util.Map;
import java.util.Random;

/**
 * Acceptance checks for shard merging, parameter compatibility and
 * serialization round-trips.
 *
 * Merged summaries are evaluated against the exact rank oracle of the UNION:
 * quantile error must stay within floor(epsilon * totalN). Incompatible
 * epsilon, unknown snapshot versions and tampered snapshots must be rejected.
 */
public final class GKMergeTest {

    private static final int GRID = 201;

    public static void main(String[] args) {
        testTwoShardsUniform();
        testTwoShardsPareto();
        testTiesAcrossShards();
        testManyShardsChainAndTree();
        testEmptyMerge();
        testRejectIncompatibleEpsilon();
        testSnapshotRoundTrip();
        testRejectBadSnapshot();
        testMergeEqualsUnionMonotonicCount();
        System.out.println("GKMergeTest: ALL PASSED");
    }

    private static GKQuantileSummary build(double[] data, double epsilon) {
        GKQuantileSummary s = new GKQuantileSummary(epsilon);
        for (double v : data) {
            s.insert(v);
        }
        s.compress();
        return s;
    }

    private static void assertQuantileBound(String tag, GKQuantileSummary s,
                                            double[] unionSorted, double epsilon) {
        long n = s.count();
        long bound = (long) Math.floor(epsilon * n);
        long maxErr = 0;
        for (int gi = 0; gi < GRID; gi++) {
            double q = (double) gi / (GRID - 1);
            long target = Math.max(1, (long) Math.ceil(q * n));
            double v = s.quantile(q);
            long rankLE = TestData.exactRankLE(unionSorted, v);
            long rankLT = GKCoreTest.exactRankLT(unionSorted, v);
            // Tie-robust GK guarantee: distance from target to the nearest
            // edge of v's rank block; zero when the answer is an exact order
            // statistic (the heavy-tie case).
            long err;
            if (rankLT <= target && target <= rankLE) {
                err = 0;
            } else {
                err = Math.min(Math.abs(rankLE - target), Math.abs(rankLT - target));
            }
            if (err > bound + 1) {
                throw new AssertionError(String.format(
                        "[%s] merged rank error %d exceeds bound %d at q=%.4f "
                                + "(target=%d, block=[%d,%d], value=%.4g, n=%d, tuples=%d)",
                        tag, err, bound, q, target, rankLT, rankLE, v, n, s.storedTuples()));
            }
            maxErr = Math.max(maxErr, err);
        }
        System.out.printf("%-26s n=%7d tuples=%6d maxErr=%d bound=%d%n",
                tag, n, s.storedTuples(), maxErr, bound);
    }

    private static double[] concat(double[]... parts) {
        int total = 0;
        for (double[] p : parts) total += p.length;
        double[] all = new double[total];
        int off = 0;
        for (double[] p : parts) {
            System.arraycopy(p, 0, all, off, p.length);
            off += p.length;
        }
        return all;
    }

    private static void testTwoShardsUniform() {
        double eps = 0.01d;
        double[] a = TestData.uniform(120_000, TestData.SEED + 10);
        double[] b = TestData.uniform(90_000, TestData.SEED + 11);
        GKQuantileSummary sa = build(a, eps);
        GKQuantileSummary sb = build(b, eps);
        GKQuantileSummary m = GKQuantileSummary.merge(sa, sb);
        m.validateInternals();
        if (m.count() != a.length + b.length) {
            throw new AssertionError("merged count mismatch");
        }
        double[] union = TestData.sorted(concat(a, b));
        assertQuantileBound("merge-uniform-2", m, union, eps);
    }

    private static void testTwoShardsPareto() {
        double eps = 0.01d;
        double[] a = TestData.pareto(150_000, 1.1d, TestData.SEED + 20);
        double[] b = TestData.pareto(150_000, 1.1d, TestData.SEED + 21);
        GKQuantileSummary m = GKQuantileSummary.merge(build(a, eps), build(b, eps));
        double[] union = TestData.sorted(concat(a, b));
        assertQuantileBound("merge-pareto-2", m, union, eps);
        // Extreme tail value must survive the merge.
        double maxA = java.util.Arrays.stream(a).max().orElseThrow();
        double maxB = java.util.Arrays.stream(b).max().orElseThrow();
        double maxUnion = Math.max(maxA, maxB);
        if (m.quantile(1.0) != maxUnion) {
            throw new AssertionError("maximum must be preserved through merge");
        }
        double minA = java.util.Arrays.stream(a).min().orElseThrow();
        double minB = java.util.Arrays.stream(b).min().orElseThrow();
        if (m.quantile(0.0) != Math.min(minA, minB)) {
            throw new AssertionError("minimum must be preserved through merge");
        }
    }

    private static void testTiesAcrossShards() {
        double eps = 0.02d;
        // Both shards contain the global minimum 0 and global maximum 1 —
        // the interval-union case that naive merges mishandle.
        double[] a = TestData.binaryTies(50_000);
        double[] b = TestData.smallAlphabet(50_000, 3, TestData.SEED + 30);
        GKQuantileSummary m = GKQuantileSummary.merge(build(a, eps), build(b, eps));
        double[] union = TestData.sorted(concat(a, b));
        assertQuantileBound("merge-ties", m, union, eps);
        // Global extrema survive a ties-heavy merge.
        if (m.quantile(0.0) != 0d) {
            throw new AssertionError("ties merge must preserve the minimum 0");
        }
    }

    private static void testManyShardsChainAndTree() {
        double eps = 0.01d;
        int shards = 8;
        int per = 40_000;
        double[][] data = new double[shards][];
        GKQuantileSummary[] sums = new GKQuantileSummary[shards];
        for (int i = 0; i < shards; i++) {
            data[i] = TestData.pareto(per, 1.2d, TestData.SEED + 40 + i);
            sums[i] = build(data[i], eps);
        }
        double[] union = TestData.sorted(concat(data));

        // Left fold: (((0+1)+2)+3)...
        GKQuantileSummary chain = sums[0];
        for (int i = 1; i < shards; i++) {
            chain = GKQuantileSummary.merge(chain, sums[i]);
        }
        assertQuantileBound("merge-pareto-chain-8", chain, union, eps);

        // Balanced tree fold: pair, pair pairs, ...
        GKQuantileSummary[] level = sums;
        while (level.length > 1) {
            GKQuantileSummary[] next = new GKQuantileSummary[(level.length + 1) / 2];
            for (int i = 0; i < level.length; i += 2) {
                next[i / 2] = (i + 1 < level.length)
                        ? GKQuantileSummary.merge(level[i], level[i + 1])
                        : level[i];
            }
            level = next;
        }
        assertQuantileBound("merge-pareto-tree-8", level[0], union, eps);

        // Inputs must be unchanged by merge.
        long sumCounts = 0;
        for (GKQuantileSummary s : sums) sumCounts += s.count();
        if (sumCounts != (long) shards * per) {
            throw new AssertionError("merge must not mutate inputs");
        }
    }

    private static void testEmptyMerge() {
        Asserts a = new Asserts("merge-empty");
        GKQuantileSummary empty = new GKQuantileSummary(0.01d);
        GKQuantileSummary full = build(TestData.uniform(10_000, 1L), 0.01d);
        GKQuantileSummary m1 = GKQuantileSummary.merge(empty, full);
        GKQuantileSummary m2 = GKQuantileSummary.merge(full, empty);
        GKQuantileSummary m3 = GKQuantileSummary.merge(empty, new GKQuantileSummary(0.01d));
        a.check(m1.count() == full.count(), "empty+full count");
        a.check(m2.count() == full.count(), "full+empty count");
        a.check(m3.count() == 0 && m3.storedTuples() == 0, "empty+empty");
        if (m1.quantile(0.5) != m2.quantile(0.5)) {
            throw new AssertionError("empty merge should not move the quantile");
        }
    }

    private static void testRejectIncompatibleEpsilon() {
        GKQuantileSummary s1 = build(TestData.uniform(5_000, 2L), 0.01d);
        GKQuantileSummary s2 = build(TestData.uniform(5_000, 3L), 0.02d);
        try {
            GKQuantileSummary.merge(s1, s2);
            throw new AssertionError("merge with different epsilon must be rejected");
        } catch (IncompatibleSummaryException expected) {
            System.out.println("rejected incompatible epsilon merge: " + expected.getMessage());
        }
    }

    private static void testSnapshotRoundTrip() {
        Asserts a = new Asserts("snapshot-roundtrip");
        double eps = 0.01d;
        double[] data = TestData.pareto(120_000, 1.3d, TestData.SEED + 50);
        GKQuantileSummary s = build(data, eps);
        Object snap = s.toSnapshot();
        String json = Json.write(snap);
        Object reparsed = Json.parse(json);
        GKQuantileSummary r = GKQuantileSummary.fromSnapshot(reparsed);
        a.check(r.count() == s.count(), "count preserved");
        a.check(Math.abs(r.epsilon() - eps) < 1e-15, "epsilon preserved");
        a.check(r.storedTuples() == s.storedTuples(), "tuple count preserved");
        for (double q = 0.0; q <= 1.0; q += 0.05) {
            if (s.quantile(q) != r.quantile(q)) {
                throw new AssertionError("quantile " + q + " differs after snapshot round trip");
            }
        }
        // A restored snapshot merges like the original.
        double[] other = TestData.pareto(80_000, 1.3d, TestData.SEED + 51);
        GKQuantileSummary m = GKQuantileSummary.merge(r, build(other, eps));
        double[] union = TestData.sorted(concat(data, other));
        assertQuantileBound("snapshot-then-merge", m, union, eps);
    }

    @SuppressWarnings("unchecked")
    private static void testRejectBadSnapshot() {
        GKQuantileSummary s = build(TestData.uniform(10_000, 9L), 0.01d);

        // Unknown algorithm/version/order.
        Map<String, Object> badAlgo = (Map<String, Object>) Json.parse(Json.write(s.toSnapshot()));
        badAlgo.put("algorithm", "tdigest");
        expectIncompatible(badAlgo, "foreign algorithm");

        Map<String, Object> badVer = (Map<String, Object>) Json.parse(Json.write(s.toSnapshot()));
        badVer.put("version", 99);
        expectIncompatible(badVer, "future version");

        Map<String, Object> badOrder = (Map<String, Object>) Json.parse(Json.write(s.toSnapshot()));
        badOrder.put("order", "string-lex");
        expectIncompatible(badOrder, "foreign ordering");

        // Tampered g sums: must fail integrity validation.
        Map<String, Object> tampered = (Map<String, Object>) Json.parse(Json.write(s.toSnapshot()));
        List<Map<String, Object>> tuples = (List<Map<String, Object>>) tampered.get("tuples");
        Map<String, Object> first = tuples.get(0);
        first.put("g", ((Number) first.get("g")).intValue() + 1);
        try {
            GKQuantileSummary.fromSnapshot(tampered);
            throw new AssertionError("tampered snapshot must be rejected");
        } catch (BadRequestException | IllegalStateException expected) {
            System.out.println("rejected tampered snapshot: " + expected.getMessage());
        }

        // Missing field.
        Map<String, Object> missing = (Map<String, Object>) Json.parse(Json.write(s.toSnapshot()));
        missing.remove("n");
        try {
            GKQuantileSummary.fromSnapshot(missing);
            throw new AssertionError("malformed snapshot must be rejected");
        } catch (BadRequestException expected) {
            System.out.println("rejected malformed snapshot: " + expected.getMessage());
        }
    }

    private static void expectIncompatible(Map<String, Object> snap, String label) {
        try {
            GKQuantileSummary.fromSnapshot(snap);
            throw new AssertionError(label + " snapshot must be rejected");
        } catch (IncompatibleSummaryException expected) {
            System.out.println("rejected " + label + " snapshot: " + expected.getMessage());
        }
    }

    private static void testMergeEqualsUnionMonotonicCount() {
        // Fuzz: random shard sizes/partitions, several distributions; every
        // merged summary must (a) keep count == sum and (b) satisfy the bound.
        Random r = new Random(TestData.SEED + 60);
        double eps = 0.02d;
        for (int trial = 0; trial < 12; trial++) {
            int shardCount = 2 + r.nextInt(6);
            double[][] shards = new double[shardCount][];
            GKQuantileSummary[] sums = new GKQuantileSummary[shardCount];
            for (int k = 0; k < shardCount; k++) {
                int size = 5_000 + r.nextInt(30_000);
                int kind = r.nextInt(3);
                shards[k] = switch (kind) {
                    case 0 -> TestData.uniform(size, r.nextLong());
                    case 1 -> TestData.pareto(size, 1.1d, r.nextLong());
                    case 2 -> TestData.smallAlphabet(size, 4 + r.nextInt(20), r.nextLong());
                    default -> throw new AssertionError();
                };
                sums[k] = build(shards[k], eps);
            }
            double[] union = TestData.sorted(concat(shards));
            GKQuantileSummary m = sums[0];
            for (int k = 1; k < shardCount; k++) {
                m = GKQuantileSummary.merge(m, sums[k]);
            }
            if (m.count() != union.length) {
                throw new AssertionError("fuzz count mismatch trial " + trial);
            }
            assertQuantileBound("fuzz-merge-trial-" + trial, m, union, eps);
        }
    }
}
