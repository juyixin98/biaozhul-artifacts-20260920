package com.example.vic.engine;

import com.example.vic.domain.Interval;
import com.example.vic.domain.RuleDef;
import com.example.vic.domain.Segment;
import com.example.vic.store.RuleStore;

import java.util.ArrayList;
import java.util.List;
import java.util.Optional;
import java.util.TreeSet;

/**
 * Computes the effective coverage of a {@link RuleStore}: at every point of the axis the
 * rule from the highest-priority version wins; equal-priority overlaps are impossible
 * because the store rejects them at write time.
 *
 * Output is a list of non-overlapping {@link Segment}s, each retaining its source
 * version and rule. Adjacent segments coming from the same rule are merged.
 */
public final class CoverageEngine {

    private CoverageEngine() {
    }

    /** Effective rule at a single point, if any rule covers it. */
    public static Optional<RuleDef> effectiveAt(RuleStore store, long point) {
        RuleDef winner = null;
        int winnerPriority = Integer.MIN_VALUE;
        for (RuleDef rule : store.rules().values()) {
            if (!rule.interval().contains(point)) {
                continue;
            }
            int priority = store.versions().get(rule.versionId()).priority();
            if (winner == null || priority > winnerPriority) {
                winner = rule;
                winnerPriority = priority;
            }
        }
        return Optional.ofNullable(winner);
    }

    /** Full overlay: non-overlapping effective segments in ascending order. */
    public static List<Segment> compute(RuleStore store) {
        TreeSet<Long> boundaries = new TreeSet<>();
        for (RuleDef rule : store.rules().values()) {
            boundaries.add(rule.interval().start());
            boundaries.add(rule.interval().end());
        }
        List<Segment> merged = new ArrayList<>();
        Long previous = null;
        for (Long boundary : boundaries) {
            if (previous != null && previous < boundary) {
                Interval elementary = new Interval(previous, boundary);
                effectiveAt(store, previous).ifPresent(winner ->
                        append(merged, new Segment(elementary.start(), elementary.end(),
                                winner.versionId(), winner.id(), winner.label())));
            }
            previous = boundary;
        }
        return merged;
    }

    private static void append(List<Segment> segments, Segment next) {
        if (!segments.isEmpty()) {
            Segment last = segments.get(segments.size() - 1);
            if (last.end() == next.start()
                    && last.ruleId().equals(next.ruleId())
                    && last.versionId().equals(next.versionId())) {
                segments.set(segments.size() - 1,
                        new Segment(last.start(), next.end(), last.versionId(), last.ruleId(), last.label()));
                return;
            }
        }
        segments.add(next);
    }
}
