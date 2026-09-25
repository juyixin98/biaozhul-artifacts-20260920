package approxheavy.demo;

import approxheavy.candidates.BoundedCandidates;
import approxheavy.cms.CountMinSketch;
import approxheavy.exact.ExactCounter;

import java.util.List;
import java.util.Map;
import java.util.Random;

/**
 * Acceptance demo: feed a skewed Zipfian stream plus a deliberate
 * hash-collision stream through both the Count-Min Sketch / bounded candidate
 * set and the exact reference counter, then report:
 *
 * <ol>
 *   <li>max and average sketch overestimate vs exact counts;</li>
 *   <li>how many keys breach the declared {@code errorUpperBound} (must be 0);</li>
 *   <li>recall of the exact top-K inside the bounded candidate set;</li>
 *   <li>the forced-collision case where a sketch overestimates measurably.</li>
 * </ol>
 *
 * <p>Run: {@code java approxheavy.demo.AccuracyDemo [seed]}.
 */
public final class AccuracyDemo {
    private AccuracyDemo() {
    }

    public static void main(String[] args) {
        long rngSeed = args.length > 0 ? Long.parseLong(args[0]) : 42L;
        Random rng = new Random(rngSeed);

        System.out.println("=== Approximate frequent-item detection: accuracy report ===");
        System.out.println("rng seed: " + rngSeed);
        skewedExperiment(rng);
        collisionExperiment();
        mergeRejectionExperiment();
    }

    // ------------------------------------------------------------- skewed data

    private static void skewedExperiment(Random rng) {
        int width = 64;
        int depth = 5;
        long seed = 7L;
        int capacity = 50;
        int cardinality = 1_000;
        int events = 100_000;
        double skew = 1.07; // Zipf exponent: very skewed head
        int k = 10;

        CountMinSketch sketch = new CountMinSketch(width, depth, seed);
        ExactCounter exact = new ExactCounter();
        BoundedCandidates candidates = new BoundedCandidates(capacity, sketch::estimate);

        double[] cdf = new double[cardinality];
        double norm = 0;
        for (int i = 0; i < cardinality; i++) {
            norm += 1.0 / Math.pow(i + 1, skew);
        }
        double acc = 0;
        for (int i = 0; i < cardinality; i++) {
            acc += (1.0 / Math.pow(i + 1, skew)) / norm;
            cdf[i] = acc;
        }

        for (int n = 0; n < events; n++) {
            String key = zipfSample(rng, cdf);
            sketch.add(key);
            exact.add(key);
            candidates.observe(key);
        }

        long bound = sketch.errorUpperBound();
        long maxOver = 0;
        long totalOver = 0;
        long breaches = 0;
        Map<String, Long> exactAll = exact.allCounts();
        for (Map.Entry<String, Long> e : exactAll.entrySet()) {
            long estimate = sketch.estimate(e.getKey());
            long over = estimate - e.getValue();
            if (over < 0) {
                throw new AssertionError("sketch UNDERESTIMATED " + e.getKey()
                        + ": estimate=" + estimate + " exact=" + e.getValue());
            }
            maxOver = Math.max(maxOver, over);
            totalOver += over;
            if (over > bound) {
                breaches++;
            }
        }

        List<Map.Entry<String, Long>> exactTop = exact.topK(k);
        List<Map.Entry<String, Long>> approxTop = candidates.topK(k);
        int found = 0;
        for (Map.Entry<String, Long> e : exactTop) {
            for (Map.Entry<String, Long> a : approxTop) {
                if (a.getKey().equals(e.getKey())) {
                    found++;
                    break;
                }
            }
        }
        double recall = (double) found / k;

        System.out.println();
        System.out.println("-- Skewed Zipf stream (skew=" + skew + ") --");
        System.out.printf("events=%d distinct=%d sketch width=%d depth=%d candidateCapacity=%d%n",
                events, cardinality, width, depth, capacity);
        System.out.printf("sketch total=%d  exact total=%d%n",
                sketch.totalCount(), exact.totalCount());
        System.out.printf("declared errorUpperBound = %d (%.3f%% of total)%n",
                bound, 100.0 * bound / sketch.totalCount());
        System.out.printf("observed max overestimate = %d, mean over distinct keys = %.3f%n",
                maxOver, (double) totalOver / cardinality);
        System.out.printf("keys over bound: %d (must be 0; bound holds per key with prob 1-2^-depth=%.4f)%n",
                breaches, 1.0 - sketch.delta());

        System.out.println("  rank  exact top-" + k + "               approx top-" + k);
        for (int i = 0; i < k; i++) {
            Map.Entry<String, Long> ex = exactTop.get(i);
            Map.Entry<String, Long> ap = i < approxTop.size() ? approxTop.get(i) : null;
            String apText = ap == null ? "--"
                    : ap.getKey() + " (est=" + ap.getValue() + ", exact=" + exact.countOf(ap.getKey()) + ")";
            System.out.printf("  %4d  %-18s exact=%-7d  %s%n",
                    i + 1, ex.getKey(), ex.getValue(), apText);
        }
        System.out.printf("candidate recall vs exact top-%d: %d/%d = %.2f (NO coverage is guaranteed)%n",
                k, found, k, recall);
        if (recall < 0.8) {
            System.out.println("WARNING: recall below 0.8 for these parameters");
        }
    }

    // ---------------------------------------------------------- forced collision

    private static void collisionExperiment() {
        // Tiny width forces collisions; two keys colliding in EVERY row produce
        // an overestimate equal to the other key's count.
        int width = 2;
        int depth = 1;
        long seed = 1L;

        CountMinSketch target = new CountMinSketch(width, depth, seed);

        String a = null;
        String b = null;
        outer:
        for (int i = 0; i < 5_000; i++) {
            String x = "item-" + i;
            int bucketX = approxheavy.hash.Hashing.bucket(
                    approxheavy.hash.Hashing.rowHash(x, seed, 0), width);
            for (int j = i + 1; j < 5_000; j++) {
                String y = "item-" + j;
                int bucketY = approxheavy.hash.Hashing.bucket(
                        approxheavy.hash.Hashing.rowHash(y, seed, 0), width);
                if (bucketX == bucketY) {
                    a = x;
                    b = y;
                    break outer;
                }
            }
        }
        if (a == null) {
            throw new AssertionError("failed to find a colliding pair");
        }

        long countA = 1_000L;
        long countB = 37L;
        for (long i = 0; i < countA; i++) {
            target.add(a);
        }
        for (long i = 0; i < countB; i++) {
            target.add(b);
        }

        long estA = target.estimate(a);
        long estB = target.estimate(b);
        long bound = target.errorUpperBound();

        System.out.println();
        System.out.println("-- Forced hash-collision experiment (width=" + width
                + ", depth=" + depth + ") --");
        System.out.println("colliding pair: " + a + " and " + b);
        System.out.printf("exact: count(%s)=%d count(%s)=%d%n", a, countA, b, countB);
        System.out.printf("sketch estimates: %s=%d (over +%d), %s=%d (over +%d)%n",
                a, estA, estA - countA, b, estB, estB - countB);
        System.out.printf("declared bound: %d; bound respected: %s%n",
                bound, (estA - countA) <= bound && (estB - countB) <= bound);

        // A wider/deeper sketch of the same stream should overestimate much less.
        CountMinSketch roomy = new CountMinSketch(512, 5, seed);
        for (long i = 0; i < countA; i++) {
            roomy.add(a);
        }
        for (long i = 0; i < countB; i++) {
            roomy.add(b);
        }
        System.out.printf("same stream at width=512 depth=5: estimate(%s)=%d estimate(%s)=%d bound=%d%n",
                a, roomy.estimate(a), b, roomy.estimate(b), roomy.errorUpperBound());
    }

    // --------------------------------------------------------- merge rejection

    private static void mergeRejectionExperiment() {
        System.out.println();
        System.out.println("-- Compatibility / merge rejection --");

        CountMinSketch base = new CountMinSketch(32, 5, 99L);
        base.add("alpha", 10);

        CountMinSketch sameShape = new CountMinSketch(32, 5, 99L);
        sameShape.add("beta", 4);
        base.mergeWith(sameShape);
        System.out.println("identical (width,depth,seed) merge accepted: total=" + base.totalCount()
                + ", estimate(alpha)=" + base.estimate("alpha")
                + ", estimate(beta)=" + base.estimate("beta"));

        reject("different seed",
                () -> base.mergeWith(new CountMinSketch(32, 5, 100L)));
        reject("different width",
                () -> base.mergeWith(new CountMinSketch(64, 5, 99L)));
        reject("different depth",
                () -> base.mergeWith(new CountMinSketch(32, 4, 99L)));
    }

    private static void reject(String label, Runnable action) {
        try {
            action.run();
            System.out.println("ERROR: merge with " + label + " was ACCEPTED (should be rejected)");
        } catch (approxheavy.cms.IncompatibleSketchException e) {
            System.out.println("merge with " + label + " rejected: " + e.getMessage());
        }
    }

    // ------------------------------------------------------------------ helpers

    private static String zipfSample(Random rng, double[] cumulative) {
        double r = rng.nextDouble();
        int lo = 0;
        int hi = cumulative.length - 1;
        while (lo < hi) {
            int mid = (lo + hi) >>> 1;
            if (cumulative[mid] < r) {
                lo = mid + 1;
            } else {
                hi = mid;
            }
        }
        return "item-" + lo;
    }
}
