package streamagg.time;

import java.time.Instant;

/** Injectable clock. Production code uses {@link SystemClock}; tests use {@link ManualClock}. */
public interface Clock {
    Instant instant();
}
