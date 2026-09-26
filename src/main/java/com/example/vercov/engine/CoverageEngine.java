package com.example.vercov.engine;

import com.example.vercov.model.EffectiveSegment;
import com.example.vercov.model.IntervalRule;
import com.example.vercov.model.Version;

import java.util.ArrayList;
import java.util.Collection;
import java.util.List;
import java.util.TreeSet;

/**
 * Computes the final effective coverage of a set of prioritized versions.
 *
 * <p>Algorithm: collect every interval boundary inside the query range,
 * evaluate each atomic span between consecutive boundaries, pick the
 * highest-priority rule covering it, then merge adjacent spans that come
 * from the same version rule. The result is a list of non-overlapping
 * {@link EffectiveSegment}s, each retaining its source version id.
 */
public final class CoverageEngine {

    private CoverageEngine() {
    }

    /**
     * Computes effective coverage over the half-open range {@code [from, to)}.
     *
     * @param versions candidate versions (any priority mix)
     * @param from     inclusive range start
     * @param to       exclusive range end, must be greater than {@code from}
     * @return ordered, non-overlapping effective segments (may be empty)
     */
    public static List<EffectiveSegment> compute(Collection<Version> versions, long from, long to) {
        if (from >= to) {
            throw new IllegalArgumentException("from must be < to, got [" + from + ", " + to + ")");
        }

        TreeSet<Long> boundaries = new TreeSet<>();
        boundaries.add(from);
        boundaries.add(to);
        for (Version version : versions) {
            for (IntervalRule rule : version.intervals()) {
                if (rule.end() > from && rule.start() < to) {
                    boundaries.add(Math.max(rule.start(), from));
                    boundaries.add(Math.min(rule.end(), to));
                }
            }
        }

        List<EffectiveSegment> merged = new ArrayList<>();
        List<Long> points = List.copyOf(boundaries);
        for (int i = 0; i + 1 < points.size(); i++) {
            long spanStart = points.get(i);
            long spanEnd = points.get(i + 1);
            Winner winner = pickWinner(versions, spanStart);
            if (winner == null) {
                continue;
            }
            appendOrMerge(merged, spanStart, spanEnd, winner);
        }
        return List.copyOf(merged);
    }

    /**
     * Rejects {@code candidate} if any of its intervals overlaps an interval
     * of an existing version with the same priority.
     *
     * @throws ConflictException on the first overlap found
     */
    public static void assertNoSamePriorityConflict(Collection<Version> existing, Version candidate) {
        for (Version other : existing) {
            if (other.priority() != candidate.priority() || other.id().equals(candidate.id())) {
                continue;
            }
            for (IntervalRule a : candidate.intervals()) {
                for (IntervalRule b : other.intervals()) {
                    if (a.overlaps(b)) {
                        throw new ConflictException(
                                "priority " + candidate.priority() + " conflict: version '"
                                        + candidate.id() + "' " + a + " overlaps version '"
                                        + other.id() + "' " + b);
                    }
                }
            }
        }
    }

    private record Winner(Version version, IntervalRule rule) {
    }

    private static Winner pickWinner(Collection<Version> versions, long point) {
        Winner best = null;
        for (Version version : versions) {
            for (IntervalRule rule : version.intervals()) {
                if (rule.contains(point)
                        && (best == null || version.priority() > best.version().priority())) {
                    best = new Winner(version, rule);
                }
            }
        }
        return best;
    }

    private static void appendOrMerge(List<EffectiveSegment> merged, long spanStart, long spanEnd,
                                      Winner winner) {
        EffectiveSegment last = merged.isEmpty() ? null : merged.get(merged.size() - 1);
        boolean extendsLast = last != null
                && last.end() == spanStart
                && last.versionId().equals(winner.version().id())
                && last.priority() == winner.version().priority()
                && java.util.Objects.equals(last.label(), winner.rule().label());
        if (extendsLast) {
            merged.set(merged.size() - 1, new EffectiveSegment(
                    last.start(), spanEnd, last.versionId(), last.priority(), last.label()));
        } else {
            merged.add(new EffectiveSegment(spanStart, spanEnd,
                    winner.version().id(), winner.version().priority(), winner.rule().label()));
        }
    }
}
