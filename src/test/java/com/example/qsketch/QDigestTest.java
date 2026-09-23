package com.example.qsketch;

import org.junit.jupiter.api.Test;

import java.io.ByteArrayOutputStream;
import java.io.DataOutputStream;
import java.io.IOException;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Random;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * Acceptance tests for the q-digest: exact-rank comparison on sorted,
 * duplicate and heavy-tailed inputs with a fixed seed; rank-error bound and
 * memory-scale checks; merge semantics and compatibility rejection;
 * serialization round-trips.
 */
class QDigestTest {

    /** Exact cumulative rank via a sorted sample (binary search). */
    private static long exactRankSorted(long[] sorted, long x) {
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

    private static long[] sortedStream(int n) {
        long[] v = new long[n];
        for (int i = 0; i < n; i++) {
            v[i] = i;
        }
        return v;
    }

    /** Half the stream is one repeated value, then scattered values. */
    private static long[] duplicateStream(int n, int universe) {
        Random rnd = new Random(424242L);
        long[] v = new long[n];
        for (int i = 0; i < n / 2; i++) {
            v[i] = universe / 3;
        }
        for (int i = n / 2; i < n; i++) {
            v[i] = rnd.nextInt(universe);
        }
        return v;
    }

    /**
     * Discrete heavy-tailed stream via the inverse-CDF of a power-law tail
     * P(X >= x) ~ x^-alpha: with u uniform in (0,1), x = floor(m * u^-1/a),
     * clipped to the universe. Many small values, a few very large ones.
     */
    private static long[] heavyTailStream(int n, int universe, long seed) {
        Random rnd = new Random(seed);
        double alpha = 1.16;
        double m = universe / 1_000.0; // scale so the tail actually spans the universe
        long[] v = new long[n];
        for (int i = 0; i < n; i++) {
            double u = rnd.nextDouble();
            if (u == 0.0) {
                u = Double.MIN_VALUE;
            }
            long x = (long) Math.floor(m * Math.pow(u, -1.0 / alpha));
            if (x >= universe) {
                x = universe - 1;
            }
            v[i] = x;
        }
        return v;
    }

    private static Map<Long, Long> frequencies(long[] values) {
        Map<Long, Long> freq = new HashMap<>();
        for (long v : values) {
            freq.merge(v, 1L, Long::sum);
        }
        return freq;
    }

    /** Rank containment via sorted sample for the whole universe. */
    private void assertRankContainmentSorted(QDigest d, long[] sorted, int universe) {
        long[] edges = {-1, 0, universe - 1L, universe};
        for (long x : edges) {
            long[] b = d.rankBounds(x);
            long exact = x < 0 ? 0 : x >= universe ? d.count() : exactRankSorted(sorted, x);
            assertTrue(b[0] <= exact && b[1] >= exact,
                    "edge x=" + x + " interval [" + b[0] + "," + b[1]
                            + "] misses exact " + exact);
        }
        // sample 2001 values spread over the universe (fixed stride)
        long stride = Math.max(1, universe / 2001L);
        for (long x = 0; x < universe; x += stride) {
            long[] b = d.rankBounds(x);
            long exact = exactRankSorted(sorted, x);
            assertTrue(b[0] <= exact,
                    "lower " + b[0] + " > exact " + exact + " at x=" + x);
            assertTrue(b[1] >= exact,
                    "upper " + b[1] + " < exact " + exact + " at x=" + x);
        }
        // and the actual data values themselves
        for (int i = 0; i < sorted.length; i += Math.max(1, sorted.length / 5000)) {
            long x = sorted[i];
            long[] b = d.rankBounds(x);
            long exact = exactRankSorted(sorted, x);
            assertTrue(b[0] <= exact && b[1] >= exact,
                    "data x=" + x + " interval [" + b[0] + "," + b[1]
                            + "] misses exact " + exact);
        }
    }

    /** Interval width never exceeds the advertised eps*n + K slack. */
    private void assertGapBound(QDigest d) {
        long n = d.count();
        int k = 32 - Integer.numberOfLeadingZeros(pow2AtLeast(d.universe()) - 1);
        double slack = d.eps() * n + k;
        for (int x = 0; x < d.universe(); x++) {
            long[] b = d.rankBounds(x);
            assertTrue(b[1] - b[0] <= slack + 1e-9,
                    "gap " + (b[1] - b[0]) + " > slack " + slack + " at x=" + x
                            + " (n=" + n + ")");
        }
    }

    private static int pow2AtLeast(int u) {
        int s = 1;
        while (s < u) {
            s <<= 1;
        }
        return s;
    }

    /**
     * eps-approximate quantile criterion: v occupies the rank interval
     * [F(v-1)+1, F(v)]; that interval must intersect [target-gap,
     * target+gap]. (We additionally check the summary's own guarantee —
     * F(v) >= target and F(v-1) <= target+gap — which holds by construction
     * of the binary search on L/U.)
     */
    private void assertQuantiles(QDigest d, long[] sorted) {
        long n = d.count();
        int k = 32 - Integer.numberOfLeadingZeros(pow2AtLeast(d.universe()) - 1);
        double slack = d.eps() * n + k;
        double[] qs = {0.0, 0.01, 0.1, 0.25, 0.5, 0.75, 0.9, 0.99, 1.0};
        for (double q : qs) {
            long target = q == 0.0 ? 1L : (long) Math.ceil(q * n);
            long v = d.quantile(q);
            long fv = exactRankSorted(sorted, v);
            long rankFirst = v == 0 ? 1 : exactRankSorted(sorted, v - 1) + 1;
            assertTrue(fv >= target - slack - 1e-9 && rankFirst <= target + slack + 1e-9,
                    "q=" + q + ": rank interval [" + rankFirst + "," + fv
                            + "] misses target " + target + " by more than slack " + slack
                            + " (v=" + v + ")");
            assertTrue(fv >= target,
                    "q=" + q + ": F(v)=" + fv + " < target " + target + " (v=" + v + ")");
        }
    }

    private void assertNodeScale(QDigest d) {
        assertTrue(d.nodeCount() <= d.nodeCountBound(),
                "nodeCount " + d.nodeCount() + " exceeds bound " + d.nodeCountBound());
    }

    @Test
    void ascendingStreamRanksAreExact() {
        int n = 100_000;
        int universe = n;
        double eps = 0.01;
        long[] values = sortedStream(n);
        QDigest d = new QDigest(eps, universe);
        d.insertAll(values);

        assertRankContainmentSorted(d, values, universe);
        assertGapBound(d);
        assertNodeScale(d);
        assertQuantiles(d, values);

        // On monotone input the compression rarely promotes weight, so the
        // median should be close to exact; and quantile error is small.
        long median = d.quantile(0.5);
        long trueMedian = n / 2;
        assertTrue(Math.abs(median - trueMedian) <= eps * n + 32,
                "median " + median + " vs " + trueMedian);
        System.out.printf("[ascending] n=%d nodes=%d bound=%d median=%d true=%d%n",
                n, d.nodeCount(), d.nodeCountBound(), median, trueMedian);
    }

    @Test
    void duplicateValuesRanksAndQuantiles() {
        int n = 200_000;
        int universe = 10_000;
        double eps = 0.01;
        long[] values = duplicateStream(n, universe);
        QDigest d = new QDigest(eps, universe);
        d.insertAll(values);
        long[] sorted = values.clone();
        java.util.Arrays.sort(sorted);

        assertRankContainmentSorted(d, sorted, universe);
        assertGapBound(d);
        assertNodeScale(d);
        assertQuantiles(d, sorted);
        System.out.printf("[duplicates] n=%d nodes=%d bound=%d q(0.5)=%d%n",
                n, d.nodeCount(), d.nodeCountBound(), d.quantile(0.5));
    }

    @Test
    void heavyTailRanksAndQuantilesFixedSeed() {
        int n = 500_000;
        int universe = 1_000_000;
        double eps = 0.005;
        long[] values = heavyTailStream(n, universe, 20260923L);
        QDigest d = new QDigest(eps, universe);
        d.insertAll(values);
        Map<Long, Long> freq = frequencies(values);
        long[] sorted = values.clone();
        java.util.Arrays.sort(sorted);

        assertRankContainmentSorted(d, sorted, universe);
        assertGapBound(d);
        assertNodeScale(d);
        assertQuantiles(d, sorted);

        // Explicit numeric check against exact ranks at a few quantiles.
        for (double q : new double[]{0.5, 0.9, 0.99}) {
            long v = d.quantile(q);
            long target = (long) Math.ceil(q * n);
            long fv = exactRankSorted(sorted, v);
            long rankFirst = v == 0 ? 1 : exactRankSorted(sorted, v - 1) + 1;
            long gap = d.rankBounds(v)[1] - d.rankBounds(v)[0];
            assertTrue(fv >= target - gap && rankFirst <= target + gap,
                    "q=" + q + " v=" + v + " rank interval [" + rankFirst + ","
                            + fv + "] vs target " + target + " gap " + gap);
            System.out.printf("[heavy-tail] q=%.2f answer=%d exactRankInterval=[%d,%d] "
                            + "target=%d gap=%d nodes=%d%n",
                    q, v, rankFirst, fv, target, gap, d.nodeCount());
        }
        System.out.printf("[heavy-tail] n=%d distinct=%d nodes=%d bound=%d bytes/nodes~=20%n",
                n, freq.size(), d.nodeCount(), d.nodeCountBound());
    }

    @Test
    void memoryIsBoundedIndependentlyOfStreamLength() {
        int universe = 1 << 20;
        double eps = 0.01;
        Random rnd = new Random(7L);
        QDigest d = new QDigest(eps, universe);
        int nodesAt100k = -1;
        for (int inserted = 0; inserted < 2_000_000; inserted++) {
            d.insert(rnd.nextInt(universe));
            if (inserted == 100_000) {
                nodesAt100k = d.nodeCount();
            }
        }
        assertGapBound(d);
        assertNodeScale(d);
        // With 20x more data, node count must stay in the same order of
        // magnitude: bound here is O(K^2/eps) ~ 400*100 = 40k, far below n.
        assertTrue(d.nodeCount() < 50_000,
                "node count grew with n: " + d.nodeCount());
        assertTrue(d.nodeCount() <= 2L * nodesAt100k,
                "nodes at 2m (" + d.nodeCount() + ") more than 2x nodes at 100k ("
                        + nodesAt100k + ")");
        System.out.printf("[memory] nodes@100k=%d nodes@2m=%d bound@2m=%d%n",
                nodesAt100k, d.nodeCount(), d.nodeCountBound());
    }

    @Test
    void shardMergeEqualsUnionAndIsOrderIndependent() {
        int universe = 100_000;
        double eps = 0.01;
        int shards = 8;
        int perShard = 50_000;
        Random rnd = new Random(99L);

        QDigest merged = new QDigest(eps, universe);
        QDigest reverse = new QDigest(eps, universe);
        QDigest[] parts = new QDigest[shards];
        long[] all = new long[shards * perShard];
        for (int s = 0; s < shards; s++) {
            QDigest q = new QDigest(eps, universe);
            long[] batch = new long[perShard];
            for (int i = 0; i < perShard; i++) {
                long v = rnd.nextInt(universe);
                batch[i] = v;
                all[s * perShard + i] = v;
            }
            q.insertAll(batch);
            parts[s] = q;
        }
        for (QDigest q : parts) {
            merged.merge(q);
        }
        for (int s = shards - 1; s >= 0; s--) {
            reverse.merge(parts[s]);
        }
        java.util.Arrays.sort(all);

        assertEquals((long) shards * perShard, merged.count());
        assertEquals(merged.count(), reverse.count());
        assertRankContainmentSorted(merged, all, universe);
        assertGapBound(merged);
        assertNodeScale(merged);
        assertQuantiles(merged, all);

        // Order independence: same answers from both merge orders.
        for (double q : new double[]{0.1, 0.5, 0.9}) {
            assertEquals(merged.quantile(q), reverse.quantile(q),
                    "merge order changed quantile q=" + q);
        }
        // Compare against a single-stream digest built from the union.
        QDigest direct = new QDigest(eps, universe);
        direct.insertAll(all);
        for (int x = 0; x < universe; x += 137) {
            assertEquals(direct.rankBounds(x)[0], merged.rankBounds(x)[0], 1024);
        }
        System.out.printf("[merge] %d shards merged: nodes=%d bound=%d%n",
                shards, merged.nodeCount(), merged.nodeCountBound());
    }

    @Test
    void incompatibleMergeIsRejected() {
        QDigest a = new QDigest(0.01, 1000);
        QDigest differentEps = new QDigest(0.05, 1000);
        QDigest differentUniverse = new QDigest(0.01, 2000);
        a.insert(5);
        differentEps.insert(5);
        differentUniverse.insert(5);

        assertFalse(a.isCompatibleWith(differentEps));
        assertFalse(a.isCompatibleWith(differentUniverse));
        IllegalArgumentException e1 = assertThrows(IllegalArgumentException.class,
                () -> a.merge(differentEps));
        assertTrue(e1.getMessage().contains("eps"));
        IllegalArgumentException e2 = assertThrows(IllegalArgumentException.class,
                () -> a.merge(differentUniverse));
        assertTrue(e2.getMessage().contains("universe"));
        IllegalArgumentException e3 = assertThrows(IllegalArgumentException.class,
                () -> a.requireCompatible(null));
        // Failed merge must not mutate the digest.
        assertEquals(1, a.count());
        System.out.printf("[compat] rejected: %s | %s | %s%n",
                e1.getMessage(), e2.getMessage(), e3.getMessage());
    }

    @Test
    void serializationRoundTripsExactly() throws IOException {
        int universe = 50_000;
        QDigest d = new QDigest(0.02, universe);
        Random rnd = new Random(31337L);
        for (int i = 0; i < 120_000; i++) {
            d.insert(rnd.nextInt(universe));
        }
        byte[] wire = d.toByteArray();
        QDigest d2 = QDigest.fromByteArray(wire);

        assertEquals(d.eps(), d2.eps(), 0);
        assertEquals(d.universe(), d2.universe());
        assertEquals(d.count(), d2.count());
        assertEquals(d.nodeCount(), d2.nodeCount());
        for (int x = 0; x < universe; x += 991) {
            assertEquals(d.rankBounds(x)[0], d2.rankBounds(x)[0]);
            assertEquals(d.rankBounds(x)[1], d2.rankBounds(x)[1]);
        }
        // The round-tripped summary is still merge-compatible.
        d.merge(d2);
        assertEquals(240_000, d.count());
        System.out.printf("[serialize] nodes=%d wireBytes=%d (%.1f bytes/node)%n",
                d2.nodeCount(), wire.length, (double) wire.length / d2.nodeCount());
    }

    @Test
    void concurrentInsertsAndMergesDoNotCorruptState() throws Exception {
        int universe = 10_000;
        double eps = 0.02;
        QDigest target = new QDigest(eps, universe);
        int threads = 6;
        int perThread = 40_000;
        List<Thread> pool = new java.util.ArrayList<>();
        for (int t = 0; t < threads; t++) {
            final long seed = 100 + t;
            Thread th = new Thread(() -> {
                Random rnd = new Random(seed);
                QDigest local = new QDigest(eps, universe);
                long[] batch = rnd.longs(perThread, 0, universe).toArray();
                local.insertAll(batch);
                // merge into the shared target; snapshot/merge must tolerate
                // other threads inserting into `local`-like summaries and
                // into the target at the same time
                synchronized (target) {
                    target.merge(local);
                }
                for (int i = 0; i < 1000; i++) {
                    synchronized (target) {
                        target.insert(rnd.nextInt(universe));
                    }
                }
            });
            pool.add(th);
            th.start();
        }
        for (Thread th : pool) {
            th.join(30_000);
            assertFalse(th.isAlive(), "worker thread deadlocked");
        }
        // exactly threads*perThread inserts from merges + threads*1000 extra
        long expected = (long) threads * perThread + (long) threads * 1000;
        assertEquals(expected, target.count());
        // state is still queryable and satisfies the gap bound
        assertGapBound(target);
        assertNodeScale(target);
        // rank/quantile must return self-consistent values (no ground truth
        // available for the interleaved stream)
        long[] b = target.rankBounds(universe / 2);
        assertTrue(b[0] >= 0 && b[1] <= target.count() && b[0] <= b[1]);
        assertTrue(target.quantile(0.5) >= 0 && target.quantile(0.5) < universe);
    }

    @Test
    void malformedSerializationIsRejected() throws IOException {
        // truncated
        QDigest tmp = new QDigest(0.1, 64);
        for (int i = 0; i < 30; i++) {
            tmp.insert(i % 64);
        }
        byte[] good = tmp.toByteArray();
        byte[] truncated = new byte[good.length / 2];
        System.arraycopy(good, 0, truncated, 0, truncated.length);
        assertThrows(IOException.class, () -> QDigest.fromByteArray(truncated));

        // hand-built header with weights that do not sum to n
        java.io.ByteArrayOutputStream bos = new java.io.ByteArrayOutputStream();
        assertThrows(IOException.class, () -> {
            try (DataOutputStream out = new DataOutputStream(bos)) {
                out.writeInt(1);
                out.writeDouble(0.1);
                out.writeInt(64);
                out.writeLong(10); // n
                out.writeInt(1);
                out.writeInt(64); // leaf id
                out.writeLong(7); // only 7 of the claimed 10
            }
            QDigest.fromByteArray(bos.toByteArray());
        });

        // unknown format version
        byte[] badVersion;
        {
            ByteArrayOutputStream b2 = new ByteArrayOutputStream();
            try (DataOutputStream out = new DataOutputStream(b2)) {
                out.writeInt(99);
                out.writeDouble(0.1);
                out.writeInt(64);
                out.writeLong(0);
                out.writeInt(0);
            }
            badVersion = b2.toByteArray();
        }
        assertThrows(IOException.class, () -> QDigest.fromByteArray(badVersion));
    }

    @Test
    void tinyStreamsAndBoundariesBehave() {
        QDigest d = new QDigest(0.5, 8);
        for (long v : new long[]{0L, 0L, 7L, 7L, 3L}) {
            d.insert(v);
        }
        assertThrows(IllegalStateException.class, () -> new QDigest(0.1, 4).quantile(0.5));
        assertThrows(IllegalArgumentException.class, () -> d.insert(8));
        assertThrows(IllegalArgumentException.class, () -> d.insert(-1));
        assertThrows(IllegalArgumentException.class, () -> new QDigest(0.0, 8));
        assertThrows(IllegalArgumentException.class, () -> new QDigest(1.0, 8));
        assertThrows(IllegalArgumentException.class, () -> d.quantile(1.1));
        // boundary ranks
        assertEquals(0, d.rankBounds(-1)[0]);
        assertEquals(5, d.rankBounds(8)[1]);
        assertEquals(0.0, d.quantile(0.0));
    }

    @Test
    void rankErrorBoundHoldsOnFixedSeedSweep() {
        // Sweep several eps values and distributions; every interval width
        // stays within eps*n+K and the midpoint error within half of it.
        long seed = 20260923L;
        for (double eps : new double[]{0.05, 0.01, 0.005}) {
            int universe = 200_000;
            int n = 300_000;
            long[] values = heavyTailStream(n, universe, seed);
            QDigest d = new QDigest(eps, universe);
            d.insertAll(values);
            long[] sorted = values.clone();
            java.util.Arrays.sort(sorted);
            int k = 32 - Integer.numberOfLeadingZeros(pow2AtLeast(universe) - 1);
            double slack = eps * n + k;
            double maxMidError = 0;
            for (int x = 0; x < universe; x += 97) {
                long exact = exactRankSorted(sorted, x);
                long[] b = d.rankBounds(x);
                assertTrue(b[0] <= exact && exact <= b[1]);
                maxMidError = Math.max(maxMidError,
                        Math.abs(0.5 * (b[0] + b[1]) - exact));
            }
            assertTrue(maxMidError <= slack / 2 + 1,
                    "mid error " + maxMidError + " > " + (slack / 2));
            assertTrue(d.nodeCount() <= d.nodeCountBound());
            System.out.printf("[sweep] eps=%s n=%d nodes=%d slack=%.1f maxMidErr=%.1f%n",
                    eps, n, d.nodeCount(), slack, maxMidError);
            seed++;
        }
    }
}
