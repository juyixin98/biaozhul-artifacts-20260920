package com.example.vercov.engine;

import com.example.vercov.model.EffectiveSegment;
import com.example.vercov.model.IntervalRule;
import com.example.vercov.model.Version;
import org.junit.jupiter.api.Test;

import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

class CoverageEngineTest {

    private static Version version(String id, int priority, long[][] ranges) {
        List<IntervalRule> rules = java.util.Arrays.stream(ranges)
                .map(r -> new IntervalRule(r[0], r[1], id + "-rule"))
                .toList();
        return new Version(id, priority, rules);
    }

    @Test
    void endpointAdjacentIntervalsSamePriorityDoNotConflict() {
        Version a = version("a", 1, new long[][]{{0, 5}});
        Version b = version("b", 1, new long[][]{{5, 10}});

        CoverageEngine.assertNoSamePriorityConflict(List.of(a), b);

        List<EffectiveSegment> segments = CoverageEngine.compute(List.of(a, b), 0, 10);
        assertEquals(2, segments.size());
        assertEquals(new EffectiveSegment(0, 5, "a", 1, "a-rule"), segments.get(0));
        assertEquals(new EffectiveSegment(5, 10, "b", 1, "b-rule"), segments.get(1));
    }

    @Test
    void samePriorityOverlapIsRejected() {
        Version a = version("a", 1, new long[][]{{0, 6}});
        Version b = version("b", 1, new long[][]{{5, 10}});

        ConflictException error = assertThrows(ConflictException.class,
                () -> CoverageEngine.assertNoSamePriorityConflict(List.of(a), b));
        assertTrue(error.getMessage().contains("priority 1"));
    }

    @Test
    void higherPriorityFullyCoversLower() {
        Version low = version("low", 1, new long[][]{{2, 8}});
        Version high = version("high", 2, new long[][]{{0, 10}});

        List<EffectiveSegment> segments = CoverageEngine.compute(List.of(low, high), 0, 10);
        assertEquals(List.of(new EffectiveSegment(0, 10, "high", 2, "high-rule")), segments);
    }

    @Test
    void higherPriorityIslandSplitsLowerIntoThreeSegments() {
        Version low = version("low", 1, new long[][]{{0, 10}});
        Version high = version("high", 2, new long[][]{{3, 6}});

        List<EffectiveSegment> segments = CoverageEngine.compute(List.of(low, high), 0, 10);
        assertEquals(List.of(
                new EffectiveSegment(0, 3, "low", 1, "low-rule"),
                new EffectiveSegment(3, 6, "high", 2, "high-rule"),
                new EffectiveSegment(6, 10, "low", 1, "low-rule")), segments);
    }

    @Test
    void queryRangeClipsRulesAndLeavesGaps() {
        Version v = version("v", 1, new long[][]{{0, 4}, {8, 12}});

        List<EffectiveSegment> segments = CoverageEngine.compute(List.of(v), 2, 10);
        assertEquals(List.of(
                new EffectiveSegment(2, 4, "v", 1, "v-rule"),
                new EffectiveSegment(8, 10, "v", 1, "v-rule")), segments);
    }

    @Test
    void adjacentSpansFromSameVersionRuleAreMerged() {
        Version v = version("v", 1, new long[][]{{0, 10}});
        Version other = version("other", 2, new long[][]{{20, 30}});

        List<EffectiveSegment> segments = CoverageEngine.compute(List.of(v, other), 0, 10);
        assertEquals(List.of(new EffectiveSegment(0, 10, "v", 1, "v-rule")), segments);
    }

    @Test
    void emptyVersionSetYieldsEmptyCoverage() {
        assertTrue(CoverageEngine.compute(List.of(), 0, 10).isEmpty());
    }

    @Test
    void invalidRangeIsRejected() {
        assertThrows(IllegalArgumentException.class,
                () -> CoverageEngine.compute(List.of(), 5, 5));
    }
}
