package hllengine.test;

import hllengine.api.ApiException;
import hllengine.hll.HllConfig;
import hllengine.hll.HllSketch;

public final class MergeCompatibilityTest implements TestRunner.Suite {

    @Override
    public void register(TestRunner.Registry r) {
        r.add("merge.rejectsDifferentPrecision", this::precisionMismatch);
        r.add("merge.rejectsDifferentSeed", this::seedMismatch);
        r.add("merge.acceptsIdenticalConfig", this::identicalConfig);
        r.add("merge.errorMessageNamesTheDifference", this::messageNamesDifference);
        r.add("merge.commutativeAndIdempotent", this::mergeAlgebra);
        r.add("merge.identityWithEmptySketch", this::emptyIdentity);
        r.add("config.rejectsUnsupportedHashIdAtConstruction", this::unsupportedHash);
        r.add("config.alphaAndStandardError", this::configValues);
    }

    private void precisionMismatch(TestRunner.Assert a) {
        HllSketch x = new HllSketch(HllConfig.of(10, 0));
        HllSketch y = new HllSketch(HllConfig.of(12, 0));
        x.offerLong(1);
        y.offerLong(2);
        boolean threw = false;
        try {
            x.merge(y);
        } catch (ApiException ae) {
            threw = true;
            a.eq(ae.code(), ApiException.INCOMPATIBLE_CONFIG, "precision mismatch code");
        }
        a.check(threw, "p=10 and p=12 must not merge");
        // The failed merge must leave x unchanged.
        a.eq(x.observedCount(), 1L, "left side untouched after rejected merge");
    }

    private void seedMismatch(TestRunner.Assert a) {
        HllSketch x = new HllSketch(HllConfig.of(12, 0));
        HllSketch y = new HllSketch(HllConfig.of(12, 99));
        y.offerLong(5);
        boolean threw = false;
        try {
            x.merge(y);
        } catch (ApiException ae) {
            threw = true;
            a.eq(ae.code(), ApiException.INCOMPATIBLE_CONFIG, "seed mismatch code");
        }
        a.check(threw, "different seeds must not merge");
    }

    private void identicalConfig(TestRunner.Assert a) {
        HllSketch x = new HllSketch(HllConfig.of(12, 0));
        HllSketch y = new HllSketch(HllConfig.of(12, 0));
        for (long k = 0; k < 1000; k++) x.offerLong(k);
        for (long k = 1000; k < 2000; k++) y.offerLong(k);
        x.merge(y);
        a.approx(x.estimate().estimatedCardinality(), 2000, 0.05, "identical config merges ~2000");
    }

    private void messageNamesDifference(TestRunner.Assert a) {
        HllConfig c1 = HllConfig.of(12, 0);
        HllConfig c2 = HllConfig.of(13, 0);
        HllConfig c3 = HllConfig.of(12, 5);
        a.check(c1.compatibilityDifference(c2).contains("precision"), "difference names precision");
        a.check(c1.compatibilityDifference(c3).contains("seed"), "difference names seed");
        a.check(c1.compatibilityDifference(c1) == null, "no difference for equal config");
    }

    private void mergeAlgebra(TestRunner.Assert a) {
        HllSketch a1 = new HllSketch(HllConfig.of(12, 0));
        HllSketch a2 = new HllSketch(HllConfig.of(12, 0));
        HllSketch b1 = new HllSketch(HllConfig.of(12, 0));
        HllSketch b2 = new HllSketch(HllConfig.of(12, 0));
        for (long k = 0; k < 777; k++) {
            a1.offerLong(k);
            a2.offerLong(k);
            b1.offerLong(k);
            b2.offerLong(k);
        }
        a1.merge(a2); // merge with identical set: idempotent
        b1.merge(b2);
        for (int k = 0; k < 4096; k++) {
            a.eq(a1.registerValue(k), b1.registerValue(k), "idempotent merge registers at " + k);
        }
        a.approx(a1.estimate().estimatedCardinality(), 777, 0.10, "duplicate union stays ~777");
    }

    private void emptyIdentity(TestRunner.Assert a) {
        HllSketch s = new HllSketch(HllConfig.of(12, 0));
        for (long k = 0; k < 333; k++) s.offerLong(k);
        double before = s.estimate().rawEstimate();
        HllSketch empty = new HllSketch(HllConfig.of(12, 0));
        s.merge(empty);
        a.eq(s.estimate().rawEstimate(), before, "merging an empty sketch changes nothing");
    }

    private void unsupportedHash(TestRunner.Assert a) {
        boolean threw = false;
        try {
            new HllConfig(12, 0, "MD5");
        } catch (ApiException ae) {
            threw = true;
            a.eq(ae.code(), ApiException.UNSUPPORTED, "unsupported hash code");
        }
        a.check(threw, "only the frozen hashId is accepted");

        threw = false;
        try {
            HllConfig.of(3, 0);
        } catch (ApiException ae) {
            threw = true;
            a.eq(ae.code(), ApiException.BAD_REQUEST, "precision range code");
        }
        a.check(threw, "precision below 4 rejected");
    }

    private void configValues(TestRunner.Assert a) {
        HllConfig c = HllConfig.of(12, 0);
        a.eq(c.m(), 4096, "m=2^12");
        a.approx(c.relativeStandardError(), 1.04 / 64.0, 1e-12, "sigma for p=12");
        HllConfig d = HllConfig.defaults();
        a.eq(d.precision(), 12, "default precision is 12");
        a.eq(d.seed(), 0, "default seed is 0");
    }
}
