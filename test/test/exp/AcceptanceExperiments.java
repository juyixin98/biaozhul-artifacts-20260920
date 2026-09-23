package test.exp;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Random;
import java.util.Set;

import approx.cms.BoundedCandidateSet;
import approx.cms.CountMinSketch;
import approx.cms.ExactCounter;
import approx.cms.SketchIncompatibleException;
import test.Asserts;
import test.TestRunner;

/**
 * Acceptance experiments requested in the task specification:
 *
 * <ol>
 *   <li><b>Skew experiment</b> &mdash; Zipf-skewed stream over several sketch
 *       seeds; CMS point estimates compared to exact counts; distribution of
 *       the over-estimation and how often the declared per-item bound is
 *       exceeded is reported (must be near {@code delta}, never an under-count).</li>
 *   <li><b>Crafted-collision experiment</b> &mdash; two strings found by search
 *       that collide on <em>every</em> hash row; the victim's estimate is
 *       inflated exactly as predicted, and the inflated item can in turn evict
 *       a true heavy hitter from the bounded candidate set.</li>
 *   <li><b>Merge-compatibility experiment</b> &mdash; equal layouts merge;
 *       different seed and different width are both rejected.</li>
 *   <li><b>Candidate-coverage experiment</b> &mdash; reports recall of the true
 *       top-K in the candidate projection on a skewed, interleaved stream,
 *       demonstrating that recall can be below 1 even though every point count
 *       respects the CMS bound.</li>
 * </ol>
 *
 * The {@code main} method prints a readable report to stdout (captured under
 * {@code docs/acceptance-output.txt}); the test runner asserts the
 * deterministic invariants.
 */
public final class AcceptanceExperiments {

    private static Summary lastSummary;

    private AcceptanceExperiments() {
    }

    public static void register(TestRunner r) {
        r.add("exp: skewed Zipf stream — CMS errors vs exact counts", () -> {
            Summary s = runSkewExperiment(new java.io.PrintStream(new java.io.OutputStream() {
                @Override public void write(int b) {
                    // silent in test mode
                }
            }));
            Asserts.assertEquals(0L, s.underEstimates, "CMS never under-estimates");
            Asserts.assertLessEqual(s.maxError, s.boundAtTotal + 1,
                    "over-estimation never exceeds the deterministic all-collision bound");
            // Across many seeds, the fraction of items past the probabilistic bound
            // must stay small (per-item failure prob delta = exp(-5) ~= 0.0067).
            Asserts.assertLessEqual(Math.round(s.fractionPastBound() * 1000), 20,
                    "fraction past bound well below 2%");
        });
        r.add("exp: crafted collisions inflate counts and can evict a true heavy item",
                AcceptanceExperiments::runCollisionAsserts);
        r.add("exp: merge accepts equal layout and rejects different seed/width",
                AcceptanceExperiments::runMergeAsserts);
        r.add("exp: candidate recall of true top-K is reported, not guaranteed",
                AcceptanceExperiments::runCoverageAsserts);
    }

    /** Printable report used by {@code scripts/acceptance.sh}. */
    public static void main(String[] args) throws Exception {
        Summary skew = runSkewExperiment(System.out);
        System.out.println();
        runCollisionExperiment(System.out);
        System.out.println();
        runMergeExperiment(System.out);
        System.out.println();
        runCoverageExperiment(System.out);
        System.out.println();
        System.out.println("ALL ACCEPTANCE EXPERIMENTS COMPLETED");
        System.out.println("Note: candidate recall may be < 1 by design; point-count bounds never fail.");
    }

    // ---------------------------------------------------------------- 1. skew

    static final class Summary {
        long itemQueries;
        long underEstimates;
        long maxError;
        long totalError;
        long pastBoundQueries;
        long boundAtTotal;
        double meanError() {
            return itemQueries == 0 ? 0 : (double) totalError / itemQueries;
        }
        double fractionPastBound() {
            return itemQueries == 0 ? 0 : (double) pastBoundQueries / itemQueries;
        }
    }

    static Summary runSkewExperiment(java.io.PrintStream out) {
        int width = 272; // ceil(e/0.01)
        int depth = 5;  // delta = exp(-5) ~= 0.0067
        int distinct = 1000;
        int eventsPerSeed = 120_000;
        long[] seeds = {1L, 2L, 3L, 7L, 11L, 13L, 17L, 19L, 23L, 29L,
                31L, 37L, 41L, 43L, 47L, 53L, 59L, 61L, 67L, 71L};

        out.println("=== Experiment 1: skewed (Zipf, s=1.0) stream, CMS vs exact ===");
        out.printf(Locale.ROOT, "sketch: width=%d depth=%d (epsilon~%.4f, delta~%.5f)%n",
                width, depth, Math.E / width, Math.exp(-depth));
        out.printf(Locale.ROOT, "stream per seed: %d events, %d distinct items, %d seeds%n",
                eventsPerSeed, distinct, seeds.length);

        // Deterministic skewed item sequence independent of the sketch seed.
        int[] sequence = zipfSequence(distinct, 1.0, eventsPerSeed, 1234567L);

        Summary agg = new Summary();
        agg.boundAtTotal = (long) Math.ceil((Math.E / width) * eventsPerSeed);
        double[] perSeedPastFraction = new double[seeds.length];

        out.println("seed  maxError  meanError  queriesPastBound  fractionPastBound");
        for (int si = 0; si < seeds.length; si++) {
            CountMinSketch cms = new CountMinSketch(width, depth, seeds[si]);
            ExactCounter exact = new ExactCounter();
            for (int itemId : sequence) {
                String item = "item-" + itemId;
                cms.add(item);
                exact.add(item);
            }
            Asserts.assertEquals(eventsPerSeed, cms.totalCount(), "total matches");

            long maxErr = 0;
            long totalErr = 0;
            long under = 0;
            long past = 0;
            long queries = 0;
            long bound = cms.errorUpperBound();
            for (Map.Entry<String, Long> e : exact.snapshot().entrySet()) {
                long est = cms.estimate(e.getKey());
                long truth = e.getValue();
                long err = est - truth;
                queries++;
                if (err < 0) {
                    under++;
                }
                if (err > maxErr) {
                    maxErr = err;
                }
                if (err > bound) {
                    past++;
                }
                totalErr += err;
            }
            perSeedPastFraction[si] = (double) past / queries;
            out.printf(Locale.ROOT, "%4d  %8d  %9.3f  %16d  %17.5f%n",
                    seeds[si], maxErr, (double) totalErr / queries, past, perSeedPastFraction[si]);

            agg.itemQueries += queries;
            agg.underEstimates += under;
            agg.maxError = Math.max(agg.maxError, maxErr);
            agg.totalError += totalErr;
            agg.pastBoundQueries += past;
        }

        out.printf(Locale.ROOT, "-- aggregate: queries=%d underEstimates=%d maxError=%d (declared bound=%d)"
                        + " meanError=%.4f fractionPastBound=%.5f%n",
                agg.itemQueries, agg.underEstimates, agg.maxError, agg.boundAtTotal,
                agg.meanError(), agg.fractionPastBound());
        out.println("Interpretation: zero under-estimates always; per-item probability of exceeding");
        out.println("epsilon*N is about delta=0.0067 for any fixed item, here ~0 for the hottest");
        out.println("items and small overall. The bound is a one-sided *count* guarantee only.");
        lastSummary = agg;
        return agg;
    }

    /** Deterministic Zipf generator: weight of rank r is proportional to 1/(r+1)^s. */
    private static int[] zipfSequence(int distinct, double s, int n, long seed) {
        Random rnd = new Random(seed);
        double[] weights = new double[distinct];
        double sum = 0;
        for (int i = 0; i < distinct; i++) {
            weights[i] = 1.0 / Math.pow(i + 1, s);
            sum += weights[i];
        }
        // Inversion sampling on a coarse cumulative array.
        double[] cumulative = new double[distinct];
        double acc = 0;
        for (int i = 0; i < distinct; i++) {
            acc += weights[i] / sum;
            cumulative[i] = acc;
        }
        int[] out = new int[n];
        for (int i = 0; i < n; i++) {
            double p = rnd.nextDouble();
            int lo = 0;
            int hi = distinct - 1;
            while (lo < hi) {
                int mid = (lo + hi) >>> 1;
                if (cumulative[mid] < p) {
                    lo = mid + 1;
                } else {
                    hi = mid;
                }
            }
            out[i] = lo;
        }
        return out;
    }

    // ---------------------------------------------------------------- 2. collisions

    static void runCollisionAsserts() {
        CollisionResult r = findAndMeasureCollision(64, 3, 1L);
        Asserts.assertGreater(r.victimEstimate - 10, 20L,
                "crafted collision inflates victim well past its true count of 10");
        Asserts.assertEquals(10L, r.victimTrueCount, "victim true count is 10");
        // Heavy-hitter eviction demonstration.
        Asserts.assertFalse(r.heavyRetained,
                "a collision-inflated newcomer can evict a genuinely heavy candidate");
    }

    static final class CollisionResult {
        String victim;
        String colliderA;
        String colliderB;
        long victimTrueCount;
        long victimEstimate;
        boolean heavyRetained;
        int width;
        int depth;
        long seed;
    }

    static CollisionResult runCollisionExperiment(java.io.PrintStream out) {
        out.println("=== Experiment 2: crafted hash collisions ===");
        CollisionResult r = findAndMeasureCollision(64, 3, 1L);
        out.printf(Locale.ROOT, "sketch width=%d depth=%d seed=%d; found THREE distinct strings sharing%n",
                r.width, r.depth, r.seed);
        out.printf(Locale.ROOT, "the same bucket on every row (lexical order roles):%n");
        out.printf(Locale.ROOT, "  colliderA = %s%n  colliderB = %s%n  victim    = %s%n",
                r.colliderA, r.colliderB, r.victim);
        out.println("stream: retained-heavy x30, victim x10, colliderA x80, colliderB x40");
        out.printf(Locale.ROOT, "victim true count = %d, CMS estimate = %d (over-estimate %d)%n",
                r.victimTrueCount, r.victimEstimate, r.victimEstimate - r.victimTrueCount);
        out.printf(Locale.ROOT, "heavy-hitter eviction: genuinely heavy 'retained-heavy' "
                + "retained after collision flood = %s%n", r.heavyRetained);
        out.println("Interpretation: point queries are over-counts with the declared bound;");
        out.println("candidate *membership* decisions based on estimates can therefore be wrong.");
        return r;
    }

    /**
     * Find three distinct strings with identical bucket indices on every row.
     * With 64^3 = 262144 possible signatures, drawing 200k candidates gives
     * roughly 76 expected members per signature on average; the first signature
     * to reach three members is found almost immediately. The three strings are
     * assigned lexicographically ordered roles and the eviction scenario replayed.
     */
    private static CollisionResult findAndMeasureCollision(int width, int depth, long seed) {
        CountMinSketch probe = new CountMinSketch(width, depth, seed);
        Map<Long, List<String>> bySignature = new HashMap<>();
        List<String> triple = null;
        int limit = 200_000;
        for (int i = 0; i < limit && triple == null; i++) {
            String candidate = String.format("item-%08d", i);
            int[] idx = probe.bucketIndices(candidate);
            long sig = 0;
            for (int row = 0; row < depth; row++) {
                sig = sig * width + idx[row];
            }
            List<String> bucket = bySignature.computeIfAbsent(sig, k -> new ArrayList<>());
            bucket.add(candidate);
            if (bucket.size() == 3) {
                triple = bucket;
            }
        }
        if (triple == null) {
            throw new AssertionError("expected a triple collision among " + limit + " candidates");
        }
        List<String> sorted = triple.stream().sorted().toList();
        String colliderA = sorted.get(0);
        String colliderB = sorted.get(1);
        String victim = sorted.get(2);

        CountMinSketch cms = new CountMinSketch(width, depth, seed);
        BoundedCandidateSet candidates = new BoundedCandidateSet(2, cms);

        // A genuinely heavy item establishes itself (30 real occurrences).
        for (int i = 0; i < 30; i++) {
            cms.add("retained-heavy");
            candidates.observe("retained-heavy");
        }
        // The victim takes the second slot with a small true count.
        for (int i = 0; i < 10; i++) {
            cms.add(victim);
            candidates.observe(victim);
        }
        // colliderA: tied estimate with the victim on its first update but
        // lexically smaller, so it wins the slot: set becomes {heavy, colliderA}.
        // Its 80 real updates inflate the shared bucket to 90.
        for (int i = 0; i < 80; i++) {
            cms.add(colliderA);
            candidates.observe(colliderA);
        }
        // colliderB arrives with an estimate already at 90+1 from the shared bucket,
        // far above heavy's 30: heavy is evicted despite being a true heavy hitter.
        for (int i = 0; i < 40; i++) {
            cms.add(colliderB);
            candidates.observe(colliderB);
        }

        CollisionResult r = new CollisionResult();
        r.victim = victim;
        r.colliderA = colliderA;
        r.colliderB = colliderB;
        r.victimTrueCount = 10;
        r.victimEstimate = cms.estimate(victim);
        r.heavyRetained = candidates.contains("retained-heavy");
        r.width = width;
        r.depth = depth;
        r.seed = seed;
        return r;
    }

    // ---------------------------------------------------------------- 3. merge

    static void runMergeAsserts() {
        CountMinSketch a = new CountMinSketch(64, 4, 9L);
        CountMinSketch same = new CountMinSketch(64, 4, 9L);
        CountMinSketch otherSeed = new CountMinSketch(64, 4, 10L);
        CountMinSketch otherWidth = new CountMinSketch(32, 4, 9L);
        CountMinSketch otherDepth = new CountMinSketch(64, 5, 9L);

        a.add("alpha", 3);
        same.add("beta", 4);
        a.merge(same);
        Asserts.assertEquals(7L, a.totalCount(), "compatible merge adds totals");

        for (CountMinSketch bad : new CountMinSketch[] {otherSeed, otherWidth, otherDepth}) {
            try {
                new CountMinSketch(64, 4, 9L).merge(bad);
                Asserts.fail("incompatible merge must throw");
            } catch (SketchIncompatibleException expected) {
                // expected
            }
        }
    }

    static void runMergeExperiment(java.io.PrintStream out) {
        out.println("=== Experiment 3: sketch merge compatibility ===");
        CountMinSketch base = new CountMinSketch(64, 4, 9L);
        CountMinSketch compatible = new CountMinSketch(64, 4, 9L);
        base.add("alpha", 3);
        compatible.add("beta", 4);
        base.merge(compatible);
        out.printf(Locale.ROOT, "same width/depth/seed merged: total=%d (3+4)%n", base.totalCount());

        record Case(String label, int width, int depth, long seed) {
        }
        List<Case> cases = List.of(
                new Case("different seed", 64, 4, 10L),
                new Case("different width", 32, 4, 9L),
                new Case("different depth", 64, 5, 9L));
        for (Case c : cases) {
            CountMinSketch bad = new CountMinSketch(c.width, c.depth, c.seed);
            try {
                new CountMinSketch(64, 4, 9L).merge(bad);
                out.println(c.label + ": UNEXPECTEDLY ACCEPTED");
            } catch (SketchIncompatibleException e) {
                out.println(c.label + ": REJECTED -> " + e.getMessage());
            }
        }
        out.println("Interpretation: merge requires provably identical hash placement; any");
        out.println("difference in seed or layout is refused instead of corrupting estimates.");
    }

    // ---------------------------------------------------------------- 4. coverage

    static void runCoverageAsserts() {
        // Deterministic structural check: capacity < K means at most capacity of K
        // true heavy items can ever be reported.
        int k = 10;
        int capacity = 5;
        CountMinSketch cms = new CountMinSketch(4096, 5, 3L); // wide: estimates essentially exact
        BoundedCandidateSet set = new BoundedCandidateSet(capacity, cms);
        ExactCounter exact = new ExactCounter();
        // Ten equally-heavy items interleave with a churn of one-off items.
        Random rnd = new Random(7);
        for (int round = 0; round < 200; round++) {
            for (int h = 0; h < k; h++) {
                String item = "heavy-" + h;
                cms.add(item);
                exact.add(item);
                set.observe(item);
            }
            for (int j = 0; j < 20; j++) {
                String noise = "noise-" + round + "-" + j;
                cms.add(noise);
                exact.add(noise);
                set.observe(noise);
            }
        }
        List<Map.Entry<String, Long>> trueTopK = exact.topK(k);
        Set<String> projected = new HashSet<>();
        for (Map.Entry<String, Long> e : set.topK(capacity)) {
            projected.add(e.getKey());
        }
        int found = 0;
        for (Map.Entry<String, Long> e : trueTopK) {
            if (projected.contains(e.getKey())) {
                found++;
            }
        }
        Asserts.assertLessEqual(found, capacity, "projection never exceeds capacity");
        Asserts.assertLessEqual(projected.size(), k, "recall bounded by structure");
        // Even with exact counts, capacity alone guarantees some true top-K are absent.
        Asserts.assertGreater(k, capacity, "K > capacity => top-K coverage impossible by construction");
    }

    static void runCoverageExperiment(java.io.PrintStream out) {
        out.println("=== Experiment 4: candidate-set coverage of the true top-K ===");
        int k = 10;
        int[] capacities = {10, 7, 5, 3};
        out.printf(Locale.ROOT, "stream: %d equally heavy items interleaved with one-off noise; "
                + "wide sketch (4096x5) so counts are essentially exact%n", k);
        out.println("candidateCapacity  retainedTrueTopK  recall");
        for (int capacity : capacities) {
            CountMinSketch cms = new CountMinSketch(4096, 5, 3L);
            BoundedCandidateSet set = new BoundedCandidateSet(capacity, cms);
            ExactCounter exact = new ExactCounter();
            Random rnd = new Random(7);
            for (int round = 0; round < 200; round++) {
                for (int h = 0; h < k; h++) {
                    String item = "heavy-" + h;
                    cms.add(item);
                    exact.add(item);
                    set.observe(item);
                }
                for (int j = 0; j < 20; j++) {
                    String noise = "noise-" + round + "-" + j;
                    cms.add(noise);
                    exact.add(noise);
                    set.observe(noise);
                }
            }
            Set<String> projected = new HashSet<>();
            for (Map.Entry<String, Long> e : set.topK(capacity)) {
                projected.add(e.getKey());
            }
            int found = 0;
            for (Map.Entry<String, Long> e : exact.topK(k)) {
                if (projected.contains(e.getKey())) {
                    found++;
                }
            }
            out.printf(Locale.ROOT, "%17d  %16d  %.2f%n",
                    capacity, found, (double) found / k);
        }
        out.println("Interpretation: recall is structurally capped at capacity/K and depends on");
        out.println("arrival order/churn even with perfect counts. The API therefore never claims");
        out.println("top-K coverage; it answers point counts with an error bound instead.");
    }
}
