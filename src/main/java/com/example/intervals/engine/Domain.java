package com.example.intervals.engine;

/**
 * A totally ordered value domain for interval endpoints.
 *
 * <p>Implementations convert between the JSON endpoint token (always a string)
 * and a concrete {@link Comparable} value, and define the canonical string
 * emitted for that value. Two domains ship with the service:
 * <ul>
 *   <li>{@code time} — ISO-8601 instants / zoned date-times, ordered on the
 *       UTC time line ({@link java.time.Instant});</li>
 *   <li>{@code version} — arbitrary version strings ordered by fixed
 *       lexicographic order ({@link String#compareTo}).</li>
 * </ul>
 */
public interface Domain<T extends Comparable<? super T>> {

    /** Stable identifier used in the {@code domain} field of requests. */
    String id();

    /** Human-readable description of the ordering and accepted tokens. */
    String description();

    /** Parses a JSON endpoint token into a domain value. */
    T parseEndpoint(String raw);

    /** Formats a domain value as the canonical JSON endpoint token. */
    String formatEndpoint(T value);
}
