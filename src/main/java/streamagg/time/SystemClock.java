package streamagg.time;

import java.time.Instant;

/** Wall-clock implementation backed by {@link Instant#now()}. */
public final class SystemClock implements Clock {
    public static final SystemClock INSTANCE = new SystemClock();

    private SystemClock() {
    }

    @Override
    public Instant instant() {
        return Instant.now();
    }
}
