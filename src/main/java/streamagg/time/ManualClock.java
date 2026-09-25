package streamagg.time;

import java.time.Instant;
import java.time.ZoneId;

/**
 * Deterministic clock for tests and replay: time only moves when a test calls
 * {@link #setTime(Instant)} or {@link #advance(java.time.Duration)}.
 */
public final class ManualClock implements Clock {

    private Instant now;

    public ManualClock() {
        this(Instant.parse("2026-01-01T00:00:00Z"));
    }

    public ManualClock(Instant start) {
        this.now = start;
    }

    @Override
    public Instant instant() {
        return now;
    }

    public void setTime(Instant t) {
        this.now = t;
    }

    public void advance(java.time.Duration d) {
        this.now = now.plus(d);
    }

    /** Adapter for code that expects a {@link java.time.Clock}. */
    public java.time.Clock javaClock(ZoneId zone) {
        return java.time.Clock.fixed(now, zone);
    }
}
