package com.example.dstexpand.engine;

import com.example.dstexpand.model.DailyRule;
import com.example.dstexpand.model.ExpandRequest;
import com.example.dstexpand.model.ExpandResponse;
import com.example.dstexpand.model.Occurrence;
import com.example.dstexpand.model.ResolutionPolicy;
import org.junit.jupiter.api.Test;

import java.time.Instant;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * Fixed local test data. America/New_York transitions in 2026 (US rules,
 * stable since 2007): spring forward 2026-03-08 02:00 EST->EDT,
 * fall back 2026-11-01 02:00 EDT->EST.
 */
class DstExpanderTest {

    private static final String NY = "America/New_York";
    private final DstExpander expander = new DstExpander();

    private static ExpandRequest request(String zone, String start, String end,
                                         List<DailyRule> rules,
                                         ResolutionPolicy gap, ResolutionPolicy overlap) {
        return new ExpandRequest(zone, start, end, rules, gap, overlap);
    }

    private static List<DailyRule> rule(String time) {
        return List.of(new DailyRule(time, null));
    }

    // ---------- spring forward (gap) ----------

    @Test
    void gapEarlierUsesPostTransitionOffset() {
        ExpandResponse r = expander.expand(request(NY, "2026-03-08", "2026-03-08",
                rule("02:30"), ResolutionPolicy.EARLIER, ResolutionPolicy.EARLIER));
        assertEquals(1, r.getOccurrences().size());
        Occurrence o = r.getOccurrences().get(0);
        assertEquals(Instant.parse("2026-03-08T06:30:00Z"), o.getUtc());
        assertEquals("GAP_EARLIER", o.getResolution());
        assertEquals("2026-03-08T01:30", o.getResolvedLocal().toString());
    }

    @Test
    void gapLaterUsesPreTransitionOffset() {
        ExpandResponse r = expander.expand(request(NY, "2026-03-08", "2026-03-08",
                rule("02:30"), ResolutionPolicy.LATER, ResolutionPolicy.EARLIER));
        Occurrence o = r.getOccurrences().get(0);
        assertEquals(Instant.parse("2026-03-08T07:30:00Z"), o.getUtc());
        assertEquals("GAP_LATER", o.getResolution());
        assertEquals("2026-03-08T03:30", o.getResolvedLocal().toString());
    }

    @Test
    void gapSkipOmitsOccurrenceAndRecordsIt() {
        ExpandResponse r = expander.expand(request(NY, "2026-03-08", "2026-03-08",
                rule("02:30"), ResolutionPolicy.SKIP, ResolutionPolicy.EARLIER));
        assertTrue(r.getOccurrences().isEmpty());
        assertEquals(1, r.getSkipped().size());
        assertEquals("GAP_SKIPPED", r.getSkipped().get(0).getReason());
        assertEquals("2026-03-08", r.getSkipped().get(0).getDate().toString());
    }

    @Test
    void gapErrorAbortsExpansion() {
        ExpansionException e = assertThrows(ExpansionException.class, () ->
                expander.expand(request(NY, "2026-03-08", "2026-03-08",
                        rule("02:30"), ResolutionPolicy.ERROR, ResolutionPolicy.EARLIER)));
        assertTrue(e.getMessage().contains("does not exist"));
    }

    @Test
    void gapBoundaryInstantsAreNormal() {
        // 01:59:59 exists (EST), 03:00:00 exists (EDT) on 2026-03-08.
        ExpandResponse r = expander.expand(request(NY, "2026-03-08", "2026-03-08",
                List.of(new DailyRule("01:59", null), new DailyRule("03:00", null)),
                ResolutionPolicy.ERROR, ResolutionPolicy.ERROR));
        assertEquals(2, r.getOccurrences().size());
        assertEquals(Instant.parse("2026-03-08T06:59:00Z"), r.getOccurrences().get(0).getUtc());
        assertEquals(Instant.parse("2026-03-08T07:00:00Z"), r.getOccurrences().get(1).getUtc());
    }

    // ---------- fall back (overlap) ----------

    @Test
    void overlapEarlierPicksFirstOccurrence() {
        ExpandResponse r = expander.expand(request(NY, "2026-11-01", "2026-11-01",
                rule("01:30"), ResolutionPolicy.LATER, ResolutionPolicy.EARLIER));
        Occurrence o = r.getOccurrences().get(0);
        assertEquals(Instant.parse("2026-11-01T05:30:00Z"), o.getUtc());
        assertEquals("OVERLAP_EARLIER", o.getResolution());
        assertEquals("2026-11-01T01:30", o.getResolvedLocal().toString());
    }

    @Test
    void overlapLaterPicksSecondOccurrence() {
        ExpandResponse r = expander.expand(request(NY, "2026-11-01", "2026-11-01",
                rule("01:30"), ResolutionPolicy.LATER, ResolutionPolicy.LATER));
        Occurrence o = r.getOccurrences().get(0);
        assertEquals(Instant.parse("2026-11-01T06:30:00Z"), o.getUtc());
        assertEquals("OVERLAP_LATER", o.getResolution());
    }

    @Test
    void overlapSkipOmitsOccurrenceAndRecordsIt() {
        ExpandResponse r = expander.expand(request(NY, "2026-11-01", "2026-11-01",
                rule("01:30"), ResolutionPolicy.LATER, ResolutionPolicy.SKIP));
        assertTrue(r.getOccurrences().isEmpty());
        assertEquals("OVERLAP_SKIPPED", r.getSkipped().get(0).getReason());
    }

    @Test
    void overlapErrorAbortsExpansion() {
        ExpansionException e = assertThrows(ExpansionException.class, () ->
                expander.expand(request(NY, "2026-11-01", "2026-11-01",
                        rule("01:30"), ResolutionPolicy.LATER, ResolutionPolicy.ERROR)));
        assertTrue(e.getMessage().contains("ambiguous"));
    }

    // ---------- cross-year ----------

    @Test
    void expansionCrossesYearBoundary() {
        ExpandResponse r = expander.expand(request(NY, "2026-12-30", "2027-01-02",
                rule("09:00"), ResolutionPolicy.LATER, ResolutionPolicy.EARLIER));
        assertEquals(4, r.getOccurrences().size());
        assertEquals("2026-12-30", r.getOccurrences().get(0).getDate().toString());
        assertEquals("2027-01-02", r.getOccurrences().get(3).getDate().toString());
        for (Occurrence o : r.getOccurrences()) {
            // EST (UTC-5) the whole window: 09:00 local == 14:00 UTC.
            assertEquals("T14:00:00Z", o.getUtc().toString().substring(10));
            assertEquals("NORMAL", o.getResolution());
        }
    }

    // ---------- sorting and de-duplication ----------

    @Test
    void occurrencesAreSortedByUtcRegardlessOfRuleOrder() {
        ExpandResponse r = expander.expand(request(NY, "2026-06-15", "2026-06-15",
                List.of(new DailyRule("17:00", "c"), new DailyRule("09:00", "a"),
                        new DailyRule("12:30", "b")),
                ResolutionPolicy.LATER, ResolutionPolicy.EARLIER));
        assertEquals(List.of("a", "b", "c"),
                r.getOccurrences().stream().map(Occurrence::getLabel).toList());
    }

    @Test
    void duplicateRulesAreRemovedAndCounted() {
        ExpandResponse r = expander.expand(request(NY, "2026-06-15", "2026-06-16",
                List.of(new DailyRule("09:00", "standup"), new DailyRule("09:00", "standup"),
                        new DailyRule("09:00", "other")),
                ResolutionPolicy.LATER, ResolutionPolicy.EARLIER));
        assertEquals(3, r.getStats().getInputRules());
        assertEquals(2, r.getStats().getUniqueRules());
        assertEquals(1, r.getStats().getDuplicateRulesRemoved());
        // 2 unique rules x 2 days, no duplicate rows.
        assertEquals(4, r.getOccurrences().size());
        assertEquals(0, r.getStats().getDuplicatesRemoved());
    }

    // ---------- zones without DST ----------

    @Test
    void fixedOffsetZoneNeedsNoPolicy() {
        ExpandResponse r = expander.expand(request("Asia/Shanghai", "2026-03-08", "2026-03-08",
                rule("08:00"), ResolutionPolicy.ERROR, ResolutionPolicy.ERROR));
        assertEquals(Instant.parse("2026-03-08T00:00:00Z"), r.getOccurrences().get(0).getUtc());
        assertEquals("NORMAL", r.getOccurrences().get(0).getResolution());
    }

    // ---------- tzdb version traceability ----------

    @Test
    void responseCarriesTzdbVersion() {
        ExpandResponse r = expander.expand(request(NY, "2026-01-01", "2026-01-01",
                rule("09:00"), null, null));
        assertTrue(r.getTzdbVersion().matches("\\d{4}[a-z]"),
                "unexpected tzdb version: " + r.getTzdbVersion());
        // Defaults documented in README: gap=LATER, overlap=EARLIER.
        assertEquals("LATER", r.getGapPolicy());
        assertEquals("EARLIER", r.getOverlapPolicy());
    }

    // ---------- validation ----------

    @Test
    void unknownZoneIsRejected() {
        assertThrows(ExpansionException.class, () ->
                expander.expand(request("Mars/Olympus_Mons", "2026-01-01", "2026-01-01",
                        rule("09:00"), null, null)));
    }

    @Test
    void invertedRangeIsRejected() {
        assertThrows(ExpansionException.class, () ->
                expander.expand(request(NY, "2026-02-01", "2026-01-01", rule("09:00"), null, null)));
    }

    @Test
    void emptyRulesAreRejected() {
        assertThrows(ExpansionException.class, () ->
                expander.expand(request(NY, "2026-01-01", "2026-01-01", List.of(), null, null)));
    }

    @Test
    void badRuleTimeIsRejected() {
        assertThrows(ExpansionException.class, () ->
                expander.expand(request(NY, "2026-01-01", "2026-01-01",
                        rule("25:00"), null, null)));
    }

    @Test
    void oversizedRangeIsRejected() {
        assertThrows(ExpansionException.class, () ->
                expander.expand(request(NY, "2020-01-01", "2031-01-01", rule("09:00"), null, null)));
    }
}
