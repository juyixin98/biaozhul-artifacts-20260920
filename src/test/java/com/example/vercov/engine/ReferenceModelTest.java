package com.example.vercov.engine;

import com.example.vercov.model.EffectiveSegment;
import com.example.vercov.model.IntervalRule;
import com.example.vercov.model.Version;
import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.List;
import java.util.Random;

import static org.junit.jupiter.api.Assertions.assertEquals;

/**
 * Point-by-point reference check on a small integer axis: for every integer
 * point in the range, a brute-force scan picks the highest-priority covering
 * rule; the engine's segments must reproduce exactly that winner at every
 * point, with no overlaps and no gaps inside produced segments.
 */
class ReferenceModelTest {

    private static final long AXIS_MIN = 0;
    private static final long AXIS_MAX = 24;

    @Test
    void engineMatchesPointByPointReferenceOnRandomInputs() {
        Random random = new Random(20260922L);
        for (int trial = 0; trial < 200; trial++) {
            List<Version> versions = randomVersions(random);
            List<EffectiveSegment> segments =
                    CoverageEngine.compute(versions, AXIS_MIN, AXIS_MAX);
            assertSegmentsMatchReference(versions, segments, "trial " + trial);
        }
    }

    @Test
    void engineMatchesReferenceOnHandPickedEndpointCases() {
        List<List<Version>> scenarios = List.of(
                // touching endpoints, same priority
                List.of(v("a", 1, 0, 5), v("b", 1, 5, 10)),
                // full coverage of a lower rule
                List.of(v("low", 1, 2, 8), v("high", 2, 0, 10)),
                // nested + adjacent mix across three priorities
                List.of(v("base", 1, 0, 24), v("mid", 2, 4, 12), v("top", 3, 6, 8)),
                // disjoint islands
                List.of(v("x", 1, 1, 3), v("y", 2, 10, 12), v("z", 3, 20, 23)));
        for (List<Version> versions : scenarios) {
            List<EffectiveSegment> segments =
                    CoverageEngine.compute(versions, AXIS_MIN, AXIS_MAX);
            assertSegmentsMatchReference(versions, segments, versions.toString());
        }
    }

    private static void assertSegmentsMatchReference(List<Version> versions,
                                                     List<EffectiveSegment> segments,
                                                     String context) {
        long previousEnd = Long.MIN_VALUE;
        for (EffectiveSegment segment : segments) {
            // segments are ordered and non-overlapping
            org.junit.jupiter.api.Assertions.assertTrue(segment.start() >= previousEnd,
                    context + ": overlap at " + segment);
            org.junit.jupiter.api.Assertions.assertTrue(segment.start() < segment.end(),
                    context + ": empty segment " + segment);
            previousEnd = segment.end();
        }

        for (long point = AXIS_MIN; point < AXIS_MAX; point++) {
            EffectiveSegment expected = referenceWinner(versions, point);
            EffectiveSegment actual = segmentAt(segments, point);
            assertEquals(expected, actual, context + ": mismatch at point " + point);
        }
    }

    /** Brute-force reference: highest-priority rule covering the point. */
    private static EffectiveSegment referenceWinner(List<Version> versions, long point) {
        Version bestVersion = null;
        IntervalRule bestRule = null;
        for (Version version : versions) {
            for (IntervalRule rule : version.intervals()) {
                if (rule.contains(point)
                        && (bestVersion == null || version.priority() > bestVersion.priority())) {
                    bestVersion = version;
                    bestRule = rule;
                }
            }
        }
        if (bestVersion == null) {
            return null;
        }
        return new EffectiveSegment(point, point + 1, bestVersion.id(),
                bestVersion.priority(), bestRule.label());
    }

    private static EffectiveSegment segmentAt(List<EffectiveSegment> segments, long point) {
        for (EffectiveSegment segment : segments) {
            if (segment.start() <= point && point < segment.end()) {
                return new EffectiveSegment(point, point + 1, segment.versionId(),
                        segment.priority(), segment.label());
            }
        }
        return null;
    }

    private static List<Version> randomVersions(Random random) {
        List<Version> versions = new ArrayList<>();
        int count = 1 + random.nextInt(4);
        for (int i = 0; i < count; i++) {
            // one version per priority level keeps inputs conflict-free
            int ruleCount = 1 + random.nextInt(3);
            List<IntervalRule> rules = new ArrayList<>();
            long cursor = AXIS_MIN + random.nextInt(4);
            for (int r = 0; r < ruleCount && cursor < AXIS_MAX - 1; r++) {
                long start = cursor;
                long end = Math.min(AXIS_MAX, start + 1 + random.nextInt(6));
                rules.add(new IntervalRule(start, end, "v" + i + "-r" + r));
                cursor = end + random.nextInt(3);
            }
            if (!rules.isEmpty()) {
                versions.add(new Version("v" + i, i + 1, rules));
            }
        }
        return versions;
    }

    private static Version v(String id, int priority, long start, long end) {
        return new Version(id, priority, List.of(new IntervalRule(start, end, id + "-rule")));
    }
}
