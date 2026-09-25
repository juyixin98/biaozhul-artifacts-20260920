package approxheavy.core;

import java.time.Instant;

/** Default wall-clock implementation used by the real HTTP service. */
public final class SystemClock implements Clock {
    @Override
    public long millis() {
        return Instant.now().toEpochMilli();
    }
}
