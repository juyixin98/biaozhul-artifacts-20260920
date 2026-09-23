package hllengine.test;

import hllengine.hll.HllConfig;
import hllengine.hll.HllEstimate;
import hllengine.hll.HllSketch;
import hllengine.hll.ValueCoding;

public final class HllSketchTest implements TestRunner.Suite {

    @Override
    public void register(TestRunner.Registry r) {
        r.add("hll.emptySketchEstimatesZero", this::emptySketch);
        r.add("hll.singleDistinctValue", this::singleValue);
        r.add("hll.duplicateInsertsCountAsOne", this::duplicates);
        r.add("hll.typeTagsPreventCrossTypeCollisions", this::typeTags);
        r.add("hll.shardMergeEqualsUnionSketch", this::shardMerge);
        r.add("hll.partitionedMergeAcrossKeys", this::partitionedMerge);
        r.add("hll.estimatesAreAccurateOverSeveralScales", this::accuracy);
        r.add("hll.observedCountTracksStreamLength", this::observedCount);
    }

    private void emptySketch(TestRunner.Assert a) {
        HllSketch s = new HllSketch(HllConfig.of(12, 0));
        HllEstimate e = s.estimate();
        a.eq(e.estimatedCardinality(), 0L, "empty estimate is 0");
        a.eq(e.observedCount(), 0L, "empty observed count");
        a.check(e.empty(), "empty flag");
        a.eq(e.zeroRegisters(), 4096, "all registers zero");
    }

    private void singleValue(TestRunner.Assert a) {
        HllSketch s = new HllSketch(HllConfig.of(12, 0));
        s.offerValue("only-one");
        HllEstimate e = s.estimate();
        a.eq(e.estimatedCardinality(), 1L, "one distinct estimates as 1");
        a.check(!e.empty(), "not empty");
    }

    private void duplicates(TestRunner.Assert a) {
        HllSketch s = new HllSketch(HllConfig.of(12, 0));
        for (int k = 0; k < 10_000; k++) {
            s.offerValue("same-value");
        }
        HllEstimate e = s.estimate();
        a.eq(e.estimatedCardinality(), 1L, "10k identical inserts -> 1 distinct");
        a.eq(e.observedCount(), 10_000L, "observed stream length is 10000");

        // Two distinct values, highly duplicated.
        HllSketch two = new HllSketch(HllConfig.of(12, 0));
        for (int k = 0; k < 5_000; k++) {
            two.offerValue("a");
            two.offerValue("b");
        }
        a.eq(two.estimate().estimatedCardinality(), 2L, "two duplicated values -> 2 distinct");
    }

    private void typeTags(TestRunner.Assert a) {
        // 1 (number), "1" (string), true (boolean) must remain three distinct values.
        HllSketch s = new HllSketch(HllConfig.of(14, 0));
        s.offerValue(1L);
        s.offerValue("1");
        s.offerValue(Boolean.TRUE);
        long est = s.estimate().estimatedCardinality();
        a.check(est >= 2 && est <= 4, "typed encodings do not collide (got " + est + ")");

        // Direct byte-encoding check.
        a.check(!java.util.Arrays.equals(ValueCoding.encode(1L), ValueCoding.encode("1")),
                "long 1 and string \"1\" encode differently");
        a.check(!java.util.Arrays.equals(ValueCoding.encode(1L), ValueCoding.encode(1.0)),
                "long 1 and double 1.0 encode differently by design");
    }

    private void shardMerge(TestRunner.Assert a) {
        // Two shards share half their keys: |A ∪ B| = 1500, |A|=|B|=1000.
        HllSketch shard1 = new HllSketch(HllConfig.of(12, 0));
        HllSketch shard2 = new HllSketch(HllConfig.of(12, 0));
        HllSketch truth = new HllSketch(HllConfig.of(12, 0));

        for (long k = 0; k < 1000; k++) {
            shard1.offerLong(k);
            truth.offerLong(k);
        }
        for (long k = 500; k < 1500; k++) {
            shard2.offerLong(k);
            truth.offerLong(k);
        }

        HllSketch merged = new HllSketch(HllConfig.of(12, 0));
        merged.merge(shard1);
        merged.merge(shard2);

        HllEstimate est = merged.estimate();
        // 3-sigma at p=12 ~= 4.9%; deterministic, use a 7% test band.
        a.approx(est.estimatedCardinality(), 1500, 0.07, "overlapping-shard union ~1500");
        // Register state must equal the directly-built union.
        for (int k = 0; k < 4096; k++) {
            a.eq(merged.registerValue(k), truth.registerValue(k),
                    "merged registers match direct union at " + k);
        }
        a.eq(merged.observedCount(), 2000L, "merged observed count sums shard stream lengths");
    }

    private void partitionedMerge(TestRunner.Assert a) {
        // 8 disjoint key ranges merged together against one single-builder sketch.
        int shards = 8;
        int perShard = 2_000;
        HllSketch[] parts = new HllSketch[shards];
        HllSketch whole = new HllSketch(HllConfig.of(12, 0));
        for (int s = 0; s < shards; s++) {
            parts[s] = new HllSketch(HllConfig.of(12, 0));
            for (int k = 0; k < perShard; k++) {
                long v = (long) s * perShard + k;
                parts[s].offerLong(v);
                whole.offerLong(v);
            }
        }
        HllSketch merged = new HllSketch(HllConfig.of(12, 0));
        for (HllSketch p : parts) merged.merge(p);
        for (int k = 0; k < 4096; k++) {
            a.eq(merged.registerValue(k), whole.registerValue(k),
                    "disjoint merge registers identical at " + k);
        }
        a.approx(merged.estimate().estimatedCardinality(),
                (long) shards * perShard, 0.06, "8-way disjoint union ~16000");
    }

    private void accuracy(TestRunner.Assert a) {
        // Deterministic fixed inputs 0..n-1. The point of this test is not the
        // distribution (the experiment measures that over many trials) but that
        // the estimator behaves sensibly. Band is ~3 nominal sigmas; the
        // observed fixed-input value at p=12/n=10k happens to be 4.28%.
        long[] cardinalities = {100L, 1_000L, 10_000L, 100_000L, 1_000_000L};
        for (long n : cardinalities) {
            HllSketch s = new HllSketch(HllConfig.of(12, 0));
            for (long k = 0; k < n; k++) s.offerLong(k);
            long est = s.estimate().estimatedCardinality();
            double rel = Math.abs(est - n) / (double) n;
            a.check(rel < 0.06,
                    "p=12 cardinality " + n + " estimated " + est + " (rel error "
                            + String.format("%.3f%%", rel * 100) + ") within 6%");
        }
        // Higher precision must use more registers and track lower nominal error.
        HllSketch p14 = new HllSketch(HllConfig.of(14, 0));
        for (long k = 0; k < 1_000_000; k++) p14.offerLong(k);
        double rel14 = Math.abs(p14.estimate().rawEstimate() - 1_000_000) / 1_000_000.0;
        a.check(rel14 < 0.01, "p=14 million-distinct rel error "
                + String.format("%.3f%%", rel14 * 100) + " within 1%");
    }

    private void observedCount(TestRunner.Assert a) {
        HllSketch s = new HllSketch(HllConfig.of(10, 0));
        for (int k = 0; k < 300; k++) s.offerValue("k" + (k % 50));
        HllEstimate e = s.estimate();
        a.eq(e.observedCount(), 300L, "observedCount counts inserts with duplication");
        a.approx(e.estimatedCardinality(), 50, 0.10, "distinct is still ~50");
    }
}
