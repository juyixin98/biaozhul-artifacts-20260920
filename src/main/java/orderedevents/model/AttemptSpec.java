package orderedevents.model;

/**
 * One simulated attempt of an event.
 *
 * <ul>
 *   <li>{@code delayMillis}: processing time of this attempt before it settles.</li>
 *   <li>{@code behavior}: what the attempt does after the delay —
 *       {@code succeed}, {@code fail}, or {@code timeout} (the attempt ignores
 *       the configured timeout and keeps "working" until the framework cuts it
 *       off at the attempt deadline).</li>
 *   <li>{@code error}: message carried by the placeholder when this attempt
 *       fails (optional, for {@code fail}/{@code timeout} behavior).</li>
 * </ul>
 *
 * The list of attempts is capped per event by the server configuration
 * ({@code maxAttempts}); if the client supplies fewer attempts the last one is
 * repeated; the default (no attempts given) is a single immediate success.
 */
public record AttemptSpec(long delayMillis, String behavior, String error) {

    public static final String SUCCEED = "succeed";
    public static final String FAIL = "fail";
    public static final String TIMEOUT = "timeout";

    public boolean isValid() {
        return delayMillis >= 0
                && (SUCCEED.equals(behavior) || FAIL.equals(behavior) || TIMEOUT.equals(behavior));
    }
}
