package com.example.vic;

import com.example.vic.domain.Interval;
import com.example.vic.domain.RuleDef;
import com.example.vic.domain.Segment;
import com.example.vic.domain.VersionDef;
import com.example.vic.engine.CoverageEngine;
import com.example.vic.store.ConflictException;
import com.example.vic.store.RuleStore;
import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Optional;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

class CoverageEngineTest {

    private static final long AXIS_MIN = 0;
    private static final long AXIS_MAX = 12;

    private static RuleStore baseStore() {
        return RuleStore.empty()
                .withVersion(new VersionDef("v1", 1))
                .withVersion(new VersionDef("v2", 2))
                .withVersion(new VersionDef("v3", 3));
    }

    /** Independent point-wise reference: scan all rules, keep the highest-priority hit. */
    private static String referenceRuleAt(RuleStore store, long point) {
        String bestRule = null;
        int bestPriority = Integer.MIN_VALUE;
        for (RuleDef rule : store.rules().values()) {
            if (rule.interval().contains(point)) {
                int priority = store.versions().get(rule.versionId()).priority();
                if (bestRule == null || priority > bestPriority) {
                    bestRule = rule.id();
                    bestPriority = priority;
                }
            }
        }
        return bestRule;
    }

    private static String segmentRuleAt(List<Segment> segments, long point) {
        for (Segment s : segments) {
            if (point >= s.start() && point < s.end()) {
                return s.ruleId();
            }
        }
        return null;
    }

    private static void assertMatchesPointWiseReference(RuleStore store) {
        List<Segment> segments = CoverageEngine.compute(store);
        for (long p = AXIS_MIN; p < AXIS_MAX; p++) {
            assertEquals(referenceRuleAt(store, p), segmentRuleAt(segments, p),
                    "mismatch at point " + p);
        }
        for (int i = 1; i < segments.size(); i++) {
            assertTrue(segments.get(i - 1).end() <= segments.get(i).start(),
                    "segments must not overlap");
        }
    }

    @Test
    void emptyStoreProducesNoSegments() {
        assertTrue(CoverageEngine.compute(RuleStore.empty()).isEmpty());
    }

    @Test
    void adjacentEndpointsDoNotOverlapAndDoNotGap() {
        RuleStore store = baseStore()
                .withRule(new RuleDef("r1", "v1", new Interval(0, 5), "left"))
                .withRule(new RuleDef("r2", "v1", new Interval(5, 10), "right"));
        List<Segment> segments = CoverageEngine.compute(store);
        assertEquals(2, segments.size());
        assertEquals(new Segment(0, 5, "v1", "r1", "left"), segments.get(0));
        assertEquals(new Segment(5, 10, "v1", "r2", "right"), segments.get(1));
        // boundary point 5 belongs to the right interval only (half-open semantics)
        assertEquals("r2", segmentRuleAt(segments, 5));
        assertEquals("r1", segmentRuleAt(segments, 4));
        assertMatchesPointWiseReference(store);
    }

    @Test
    void higherPriorityFullyCoversLowerPriority() {
        RuleStore store = baseStore()
                .withRule(new RuleDef("r1", "v1", new Interval(0, 10), "base"))
                .withRule(new RuleDef("r2", "v2", new Interval(2, 8), "override"));
        List<Segment> segments = CoverageEngine.compute(store);
        assertEquals(List.of(
                new Segment(0, 2, "v1", "r1", "base"),
                new Segment(2, 8, "v2", "r2", "override"),
                new Segment(8, 10, "v1", "r1", "base")), segments);
        assertMatchesPointWiseReference(store);
    }

    @Test
    void nestedThreeLevelOverlayKeepsSourceVersion() {
        RuleStore store = baseStore()
                .withRule(new RuleDef("r1", "v1", new Interval(0, 12), "L1"))
                .withRule(new RuleDef("r2", "v2", new Interval(2, 10), "L2"))
                .withRule(new RuleDef("r3", "v3", new Interval(4, 6), "L3"));
        List<Segment> segments = CoverageEngine.compute(store);
        assertEquals(List.of(
                new Segment(0, 2, "v1", "r1", "L1"),
                new Segment(2, 4, "v2", "r2", "L2"),
                new Segment(4, 6, "v3", "r3", "L3"),
                new Segment(6, 10, "v2", "r2", "L2"),
                new Segment(10, 12, "v1", "r1", "L1")), segments);
        assertMatchesPointWiseReference(store);
    }

    @Test
    void deletingVersionRecomputesAndRestoresCoveredSegments() {
        RuleStore withOverride = baseStore()
                .withRule(new RuleDef("r1", "v1", new Interval(0, 10), "base"))
                .withRule(new RuleDef("r2", "v2", new Interval(2, 8), "override"));
        RuleStore afterDelete = withOverride.withoutVersion("v2");
        List<Segment> segments = CoverageEngine.compute(afterDelete);
        assertEquals(List.of(new Segment(0, 10, "v1", "r1", "base")), segments);
        assertMatchesPointWiseReference(afterDelete);
    }

    @Test
    void deletingSingleRuleLeavesGap() {
        RuleStore store = baseStore()
                .withRule(new RuleDef("r1", "v1", new Interval(0, 5), "a"))
                .withRule(new RuleDef("r2", "v1", new Interval(5, 10), "b"))
                .withoutRule("r1");
        List<Segment> segments = CoverageEngine.compute(store);
        assertEquals(List.of(new Segment(5, 10, "v1", "r2", "b")), segments);
        assertNull(segmentRuleAt(segments, 3));
        assertMatchesPointWiseReference(store);
    }

    @Test
    void equalPriorityOverlapIsRejected() {
        RuleStore store = baseStore().withRule(new RuleDef("r1", "v1", new Interval(0, 6), "a"));
        ConflictException e = assertThrows(ConflictException.class, () ->
                store.withRule(new RuleDef("r2", "v1", new Interval(4, 10), "b")));
        assertTrue(e.getMessage().contains("equal-priority conflict"));
    }

    @Test
    void equalPriorityAcrossDifferentVersionsIsRejected() {
        RuleStore store = baseStore()
                .withVersion(new VersionDef("v1b", 1))
                .withRule(new RuleDef("r1", "v1", new Interval(0, 6), "a"));
        // touching endpoints [0,6) and [6,10) must NOT conflict; real overlap must
        RuleStore ok = store.withRule(new RuleDef("r2", "v1b", new Interval(6, 10), "touching"));
        assertEquals(2, ok.rules().size());
        assertThrows(ConflictException.class, () ->
                store.withRule(new RuleDef("r3", "v1b", new Interval(5, 7), "overlap")));
    }

    @Test
    void duplicateRuleIdIsRejected() {
        RuleStore store = baseStore().withRule(new RuleDef("r1", "v1", new Interval(0, 5), null));
        assertThrows(ConflictException.class, () ->
                store.withRule(new RuleDef("r1", "v2", new Interval(6, 9), null)));
    }

    @Test
    void invalidIntervalIsRejected() {
        assertThrows(IllegalArgumentException.class, () -> new Interval(5, 5));
        assertThrows(IllegalArgumentException.class, () -> new Interval(7, 3));
    }

    @Test
    void effectiveAtReturnsHighestPriorityRule() {
        RuleStore store = baseStore()
                .withRule(new RuleDef("r1", "v1", new Interval(0, 10), "base"))
                .withRule(new RuleDef("r2", "v2", new Interval(2, 8), "override"));
        Optional<RuleDef> at3 = CoverageEngine.effectiveAt(store, 3);
        assertTrue(at3.isPresent());
        assertEquals("r2", at3.get().id());
        assertTrue(CoverageEngine.effectiveAt(store, 9).isPresent());
        assertEquals("r1", CoverageEngine.effectiveAt(store, 9).get().id());
        assertTrue(CoverageEngine.effectiveAt(store, 10).isEmpty());
    }

    @Test
    void storeIsImmutableAfterWrites() {
        RuleStore before = baseStore().withRule(new RuleDef("r1", "v1", new Interval(0, 5), "a"));
        RuleStore after = before.withRule(new RuleDef("r2", "v2", new Interval(1, 4), "b"));
        assertEquals(1, before.rules().size());
        assertEquals(2, after.rules().size());
        assertEquals(1, CoverageEngine.compute(before).size());
    }
}
