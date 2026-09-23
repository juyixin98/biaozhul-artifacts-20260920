package seqcep.engine;

import java.util.Objects;

/**
 * A single input event.
 *
 * @param type      event type; only {@code "A"}, {@code "B"}, {@code "C"} participate in
 *                  the pattern, any other type is an irrelevant event that must be skipped
 * @param entityId  grouping key; pattern state is kept per entity and never crosses entities
 * @param timestamp event time in epoch milliseconds. The A&rarr;B&rarr;C chain must complete
 *                  within {@link MatchEngine#WINDOW_MS} (10&nbsp;000 ms) measured from A
 * @param seq       global, strictly increasing input ordinal assigned by the engine.
 *                  When two events share a timestamp, the one with the smaller seq is
 *                  considered earlier (tie-break rule required by the specification)
 */
public record Event(long seq, String type, String entityId, long timestamp) {

    public Event {
        Objects.requireNonNull(type, "type");
        Objects.requireNonNull(entityId, "entityId");
    }

    /** Types that participate in the A&rarr;B&rarr;C pattern. */
    public static boolean isPatternType(String type) {
        return "A".equals(type) || "B".equals(type) || "C".equals(type);
    }
}
