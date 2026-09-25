package streamagg.model;

import java.math.BigDecimal;

/** Per-key running aggregate: sum of values and count of live events. */
public final class Aggregate {
    private BigDecimal sum = BigDecimal.ZERO;
    private long count;

    public BigDecimal sum() {
        return sum;
    }

    public long count() {
        return count;
    }

    public void add(BigDecimal v) {
        sum = sum.add(v);
        count++;
    }

    public void remove(BigDecimal v) {
        sum = sum.subtract(v);
        count--;
    }

    /**
     * Invariant guard: count must never drift below zero. A negative count means a
     * retract was applied to a key with no live event (a bug in the engine).
     *
     * @throws IllegalStateException if count would become negative
     */
    public void assertNoNegativeDrift() {
        if (count < 0) {
            throw new IllegalStateException("negative count drift detected: " + count);
        }
    }
}
