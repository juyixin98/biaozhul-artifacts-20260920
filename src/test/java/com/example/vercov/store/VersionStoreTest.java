package com.example.vercov.store;

import com.example.vercov.engine.ConflictException;
import com.example.vercov.model.EffectiveSegment;
import com.example.vercov.model.IntervalRule;
import com.example.vercov.model.Version;
import org.junit.jupiter.api.Test;

import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

class VersionStoreTest {

    private static Version version(String id, int priority, long start, long end) {
        return new Version(id, priority, List.of(new IntervalRule(start, end, id + "-rule")));
    }

    @Test
    void deletingAVersionRecomputesCoverageAndRestoresShadowedRules() {
        VersionStore store = new VersionStore();
        store.add(version("base", 1, 0, 10));
        store.add(version("patch", 2, 3, 6));

        assertEquals(List.of(
                new EffectiveSegment(0, 3, "base", 1, "base-rule"),
                new EffectiveSegment(3, 6, "patch", 2, "patch-rule"),
                new EffectiveSegment(6, 10, "base", 1, "base-rule")),
                store.coverage(0, 10));

        assertTrue(store.remove("patch"));

        assertEquals(List.of(new EffectiveSegment(0, 10, "base", 1, "base-rule")),
                store.coverage(0, 10));
    }

    @Test
    void samePriorityConflictIsRejectedAtomically() {
        VersionStore store = new VersionStore();
        store.add(version("a", 1, 0, 6));

        assertThrows(ConflictException.class, () -> store.add(version("b", 1, 5, 10)));
        // failed insert left no trace
        assertTrue(store.find("b").isEmpty());
        assertEquals(1, store.list().size());
    }

    @Test
    void duplicateIdIsRejected() {
        VersionStore store = new VersionStore();
        store.add(version("a", 1, 0, 5));

        assertThrows(VersionStore.DuplicateVersionException.class,
                () -> store.add(version("a", 2, 0, 5)));
    }

    @Test
    void pointQueryReturnsEffectiveWinner() {
        VersionStore store = new VersionStore();
        store.add(version("base", 1, 0, 10));
        store.add(version("patch", 2, 3, 6));

        assertEquals("patch", store.point(4).orElseThrow().versionId());
        assertEquals("base", store.point(8).orElseThrow().versionId());
        assertTrue(store.point(12).isEmpty());
    }

    @Test
    void versionWithSelfOverlappingIntervalsIsRejected() {
        assertThrows(IllegalArgumentException.class, () -> new Version("bad", 1,
                List.of(new IntervalRule(0, 5, null), new IntervalRule(4, 9, null))));
    }

    @Test
    void invalidIntervalIsRejected() {
        assertThrows(IllegalArgumentException.class, () -> new IntervalRule(5, 5, null));
        assertThrows(IllegalArgumentException.class, () -> new IntervalRule(7, 2, null));
    }
}
