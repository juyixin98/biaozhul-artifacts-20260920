package hllengine.hll;

import java.util.LinkedHashMap;
import java.util.Map;

/**
 * Result of a cardinality estimation. Fields are named to make it explicit
 * that the number is an estimate with an error scale &mdash; never an exact
 * distinct count.
 *
 * @param rawEstimate        unrounded estimator output
 * @param estimatedCardinality rounded point estimate
 * @param relativeStandardError nominal 1.04/&radic;m (a-priori sigma, not a bound)
 * @param observedCount      exact number of inserted values (stream length)
 * @param empty              whether the sketch was empty
 * @param zeroRegisters      registers still at zero (for diagnostics)
 * @param registers          total register count m
 */
public record HllEstimate(double rawEstimate,
                          long estimatedCardinality,
                          double relativeStandardError,
                          long observedCount,
                          boolean empty,
                          int zeroRegisters,
                          int registers) {

    public Map<String, Object> toMap() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("estimatedCardinality", estimatedCardinality);
        m.put("estimatedCardinalityRaw", rawEstimate);
        m.put("isEstimate", true);
        m.put("isExact", false);
        m.put("nominalRelativeStandardError", relativeStandardError);
        m.put("nominalRelativeStandardErrorPercent", relativeStandardError * 100.0);
        m.put("observedCount", observedCount);
        m.put("empty", empty);
        m.put("zeroRegisters", zeroRegisters);
        m.put("registers", registers);
        return m;
    }
}
