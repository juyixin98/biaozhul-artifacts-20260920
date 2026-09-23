package com.example.drvb.core;

import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertInstanceOf;
import static org.junit.jupiter.api.Assertions.assertNotEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

class RuleRegistryTest {

    private Rule rule(String id, int threshold) {
        return new Rule(id, id, "payment",
                new Condition.Compare("gt", "amount", threshold),
                "BLOCK", true);
    }

    private RuleVersion version(String id, long from, int threshold) {
        return new RuleVersion(id, from, 0L, "v" + id,
                List.of(rule("r" + threshold, threshold)));
    }

    @Test
    void missingVersionBeforeBootstrapIsNeverSilentlyFilled() {
        RuleRegistry reg = new RuleRegistry();
        assertFalse(reg.isBootstrapped());
        assertInstanceOf(RuleRegistry.Missing.class, reg.resolve(123L));
        assertThrows(RuleRegistryException.class,
                () -> reg.publish(version("v2", 100L, 10)));
    }

    @Test
    void bootstrapAndIntervalBinding() {
        RuleRegistry reg = new RuleRegistry();
        RuleVersion v1 = reg.bootstrap(version("v1", 999_999L, 10));
        assertEquals(Long.MIN_VALUE, v1.effectiveFrom(),
                "bootstrap effectiveFrom normalized to -inf");

        // event exactly at an interval boundary belongs to the NEW version:
        // binding interval is [from, nextFrom)
        RuleVersion v2 = reg.publish(version("v2", 100L, 20));
        // ... but publishing requires watermark gating; watermark is MIN here,
        // and 100 > MIN_VALUE, so it succeeds.
        assertEquals("v1", ((RuleRegistry.Bound) reg.resolve(99L)).version().versionId());
        assertEquals("v2", ((RuleRegistry.Bound) reg.resolve(100L)).version().versionId());
        assertEquals("v2", ((RuleRegistry.Bound) reg.resolve(101L)).version().versionId());
    }

    @Test
    void intervalsMustBeOrderedAndNonOverlapping() {
        RuleRegistry reg = new RuleRegistry();
        reg.bootstrap(version("v1", 0L, 10));
        // watermark does not gate publication: versions may be pre-registered
        reg.noteWatermark(1000L);
        reg.publish(version("v2", 500L, 20));

        RuleRegistryException e = assertThrows(RuleRegistryException.class,
                () -> reg.publish(version("v3", 500L, 30)));
        assertEquals(RuleErrorCode.EFFECTIVE_TIME_IN_PAST, e.code());
        RuleRegistryException e2 = assertThrows(RuleRegistryException.class,
                () -> reg.publish(version("v4", 499L, 30)));
        assertEquals(RuleErrorCode.EFFECTIVE_TIME_IN_PAST, e2.code());
        // strictly greater, not equal
        assertEquals("v2",
                ((RuleRegistry.Bound) reg.resolve(500L)).version().versionId());
    }

    @Test
    void duplicateIdsAndDoubleBootstrapRejected() {
        RuleRegistry reg = new RuleRegistry();
        reg.bootstrap(version("v1", 0L, 10));
        assertThrows(RuleRegistryException.class,
                () -> reg.bootstrap(version("v0", 0L, 10)));
        RuleRegistryException e = assertThrows(RuleRegistryException.class,
                () -> reg.publish(version("v1", 5_000L, 20)));
        assertEquals(RuleErrorCode.DUPLICATE_VERSION, e.code());
    }

    @Test
    void rollbackAppendsImmutableCopyWithSameChecksum() {
        RuleRegistry reg = new RuleRegistry();
        RuleVersion v1 = reg.bootstrap(version("v1", 0L, 10));
        RuleVersion v2 = reg.publish(version("v2", 1_000L, 20));
        RuleVersion v3 = reg.publish(version("v3", 2_000L, 30));

        RuleVersion rb = reg.rollback("v1", "rb1", 3_000L, 5_000L, "restore strict");
        assertEquals("rb1", rb.versionId());
        assertEquals(v1.checksum(), rb.checksum(), "content restored => same checksum");
        assertNotEquals(v1.versionId(), rb.versionId());
        // originals are untouched and still bind their intervals
        assertEquals("v2", ((RuleRegistry.Bound) reg.resolve(1_500L)).version().versionId());
        assertEquals("rb1", ((RuleRegistry.Bound) reg.resolve(3_000L)).version().versionId());
        // matches the restored content, not v3's threshold-30 rule
        Event e = new Event("e", "payment", 3_000L, Map.of("amount", 25));
        assertTrue(rb.rules().get(0).matches(e), "25 > restored threshold 10");
        assertFalse(v3.rules().get(0).matches(e), "25 <= v3 threshold 30");
    }

    @Test
    void rollbackOfUnknownOrReclaimedVersionRejected() {
        RuleRegistry reg = new RuleRegistry();
        reg.bootstrap(version("v1", 0L, 10));
        RuleRegistryException e = assertThrows(RuleRegistryException.class,
                () -> reg.rollback("ghost", "rb1", 5_000L, 1L, null));
        assertEquals(RuleErrorCode.SOURCE_VERSION_NOT_FOUND, e.code());
    }

    @Test
    void garbageCollectionNeedsBothHorizonAndZeroReferences() {
        RuleRegistry reg = new RuleRegistry();
        reg.bootstrap(version("v1", 0L, 10));
        reg.publish(version("v2", 1_000L, 20));
        reg.publish(version("v3", 2_000L, 30));
        reg.noteWatermark(5_000L);

        long allowedLateness = 1_000L; // horizon = 4000
        // v1 reclaimable only if nothing references it
        List<RuleVersion> kept =
                reg.reclaim(5_000L, allowedLateness, java.util.Set.of("v1"));
        assertTrue(kept.isEmpty(), "referenced version must survive");
        assertEquals("v1", reg.get("v1").versionId());

        // Without references the prefix (v1, whose successor starts at 1000 <= 4000)
        // is collected; v2 stays because v3 starts at 2000... 2000 <= 4000 too,
        // so v2 is also eligible; v3 (latest) always remains.
        List<RuleVersion> removed =
                reg.reclaim(5_000L, allowedLateness, java.util.Set.of());
        assertEquals(List.of("v1", "v2"),
                removed.stream().map(RuleVersion::versionId).toList());

        // Now an event in v1's old interval is Reclaimed, never silently
        // re-bound to v3.
        RuleRegistry.Resolution res = reg.resolve(500L);
        assertInstanceOf(RuleRegistry.Reclaimed.class, res);
        // events at/after v3's start still bind v3
        assertEquals("v3", ((RuleRegistry.Bound) reg.resolve(2_000L)).version().versionId());
    }

    @Test
    void gcPreviewDoesNotMutate() {
        RuleRegistry reg = new RuleRegistry();
        reg.bootstrap(version("v1", 0L, 10));
        reg.publish(version("v2", 1_000L, 20));
        reg.noteWatermark(5_000L);
        List<RuleVersion> preview =
                reg.reclaimPreview(5_000L, 1_000L, java.util.Set.of());
        assertEquals(1, preview.size());
        assertEquals(2, reg.versions().size(), "preview must not remove anything");
    }
}
