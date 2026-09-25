package approxheavy.core;

/** Mutable clock driven explicitly by tests. */
public final class ManualClock implements Clock {
    private long nowMillis;

    public ManualClock() {
        this(0L);
    }

    public ManualClock(long startMillis) {
        this.nowMillis = startMillis;
    }

    @Override
    public long millis() {
        return nowMillis;
    }

    public void setTime(long millis) {
        this.nowMillis = millis;
    }

    public void advance(long deltaMillis) {
        if (deltaMillis < 0) {
            throw new IllegalArgumentException("delta must be non-negative");
        }
        this.nowMillis += deltaMillis;
    }
}
