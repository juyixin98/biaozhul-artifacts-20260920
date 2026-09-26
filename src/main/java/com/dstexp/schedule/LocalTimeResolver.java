package com.dstexp.schedule;

import com.dstexp.model.GapStrategy;
import com.dstexp.model.OverlapStrategy;

import java.time.Instant;
import java.time.LocalDateTime;
import java.time.ZoneOffset;
import java.time.zone.ZoneRules;
import java.time.zone.ZoneOffsetTransition;
import java.util.Optional;

/**
 * Maps a local wall-clock date-time to a UTC instant for a zone, applying configurable
 * policies when the local time falls in a spring-forward gap or a fall-back overlap.
 */
public final class LocalTimeResolver {

    /** Outcome kind, mirroring the {@code kind} field on an occurrence. */
    public enum Kind {
        NORMAL, GAP_EARLIER, GAP_LATER, OVERLAP_EARLIER, OVERLAP_LATER
    }

    /** Result of a resolution attempt. */
    public record Result(Instant instant, ZoneOffset offset, Kind kind) {
    }

    /** Thrown when policy is ERROR and an ambiguous/non-existent local time is hit. */
    public static class AmbiguousTimeException extends RuntimeException {
        public final boolean gap;

        public AmbiguousTimeException(boolean gap, String message) {
            super(message);
            this.gap = gap;
        }
    }

    private LocalTimeResolver() {
    }

    /**
     * Resolves one local date-time.
     *
     * @return the resolved instant, or empty when the policy is SKIP.
     */
    public static Optional<Result> resolve(LocalDateTime local,
                                           ZoneRules rules,
                                           GapStrategy gapStrategy,
                                           OverlapStrategy overlapStrategy) {
        // getTransition returns non-null exactly when the local date-time falls inside a
        // gap or an overlap (boundary instants resolve normally).
        ZoneOffsetTransition transition = rules.getTransition(local);

        if (transition != null && transition.isGap()) {
            return resolveGap(local, transition, gapStrategy);
        }
        if (transition != null && transition.isOverlap()) {
            return resolveOverlap(local, transition, overlapStrategy);
        }

        ZoneOffset offset = rules.getOffset(local);
        return Optional.of(new Result(local.toInstant(offset), offset, Kind.NORMAL));
    }

    private static Optional<Result> resolveGap(LocalDateTime local,
                                               ZoneOffsetTransition t,
                                               GapStrategy strategy) {
        String display = local + " in gap [" + t.getDateTimeBefore() + ".." + t.getDateTimeAfter() + ")";
        return switch (strategy) {
            // Interpret with the post-gap offset -> the local time reads as already shifted,
            // landing just before the gap on the UTC timeline.
            case EARLIER -> Optional.of(new Result(
                    local.toInstant(t.getOffsetAfter()), t.getOffsetAfter(), Kind.GAP_EARLIER));
            // Interpret with the pre-gap offset -> lands just after the gap.
            case LATER -> Optional.of(new Result(
                    local.toInstant(t.getOffsetBefore()), t.getOffsetBefore(), Kind.GAP_LATER));
            case SKIP -> Optional.empty();
            case ERROR -> throw new AmbiguousTimeException(true,
                    "Non-existent local time " + display + " and gapPolicy=error");
        };
    }

    private static Optional<Result> resolveOverlap(LocalDateTime local,
                                                   ZoneOffsetTransition t,
                                                   OverlapStrategy strategy) {
        String display = local + " in overlap [" + t.getDateTimeAfter() + ".." + t.getDateTimeBefore() + "]";
        return switch (strategy) {
            // First pass: summer/daylight offset in effect before the transition.
            case EARLIER -> Optional.of(new Result(
                    local.toInstant(t.getOffsetBefore()), t.getOffsetBefore(), Kind.OVERLAP_EARLIER));
            // Second pass: standard offset in effect after the transition.
            case LATER -> Optional.of(new Result(
                    local.toInstant(t.getOffsetAfter()), t.getOffsetAfter(), Kind.OVERLAP_LATER));
            case SKIP -> Optional.empty();
            case ERROR -> throw new AmbiguousTimeException(false,
                    "Ambiguous local time " + display + " and overlapPolicy=error");
        };
    }
}
