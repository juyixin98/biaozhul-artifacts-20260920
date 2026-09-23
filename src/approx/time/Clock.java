package approx.time;

/** Injectable source of the current time (milliseconds since the epoch). */
public interface Clock {
    long nowMillis();
}
