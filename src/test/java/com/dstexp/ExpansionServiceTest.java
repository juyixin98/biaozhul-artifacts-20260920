package com.dstexp;

import com.dstexp.model.ExpandRequest;
import com.dstexp.model.ExpandResponse;
import com.dstexp.model.Occurrence;
import com.dstexp.model.RuleSpec;
import com.dstexp.service.ExpansionService;
import com.dstexp.service.RequestValidationException;
import com.dstexp.schedule.LocalTimeResolver;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Nested;
import org.junit.jupiter.api.Test;

import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

class ExpansionServiceTest {

    private final ExpansionService service = new ExpansionService();

    private static RuleSpec daily(String id, String localTime) {
        RuleSpec spec = new RuleSpec();
        spec.setRuleId(id);
        spec.setType("daily");
        spec.setLocalTime(localTime);
        return spec;
    }

    private static RuleSpec cron(String id, String cron) {
        RuleSpec spec = new RuleSpec();
        spec.setRuleId(id);
        spec.setType("cron");
        spec.setCron(cron);
        return spec;
    }

    private static ExpandRequest request(String zone, String from, String to, String gap,
                                         String overlap, RuleSpec... rules) {
        ExpandRequest r = new ExpandRequest();
        r.setZoneId(zone);
        r.setFromDate(from);
        r.setToDate(to);
        r.setRules(List.of(rules));
        r.setGapPolicy(gap);
        r.setOverlapPolicy(overlap);
        r.setSort(true);
        r.setDeduplicate(true);
        return r;
    }

    private static List<String> utcList(ExpandResponse response) {
        return response.occurrences().stream().map(Occurrence::utcInstant).toList();
    }

    private static List<String> kindList(ExpandResponse response) {
        return response.occurrences().stream().map(Occurrence::kind).toList();
    }

    @Nested
    @DisplayName("Spring forward gap")
    class SpringGap {

        @Test
        @DisplayName("earlier maps 02:30 to 06:30Z using -04:00 and is tagged GAP_EARLIER")
        void gapEarlier() {
            var resp = service.expand(request(
                    "America/New_York", "2026-03-08", "2026-03-08",
                    "earlier", "later", daily("r", "02:30")));

            assertThat(utcList(resp)).containsExactly("2026-03-08T06:30:00Z");
            assertThat(kindList(resp)).containsExactly("GAP_EARLIER");
            assertThat(resp.occurrences().get(0).offsetSeconds()).isEqualTo(-4 * 3600);
        }

        @Test
        @DisplayName("later maps 02:30 to 07:30Z using -05:00 and is tagged GAP_LATER")
        void gapLater() {
            var resp = service.expand(request(
                    "America/New_York", "2026-03-08", "2026-03-08",
                    "later", "later", daily("r", "02:30")));

            assertThat(utcList(resp)).containsExactly("2026-03-08T07:30:00Z");
            assertThat(kindList(resp)).containsExactly("GAP_LATER");
            assertThat(resp.occurrences().get(0).offsetSeconds()).isEqualTo(-5 * 3600);
        }

        @Test
        @DisplayName("skip drops the occurrence and records it as GAP_SKIPPED")
        void gapSkip() {
            var resp = service.expand(request(
                    "America/New_York", "2026-03-08", "2026-03-08",
                    "skip", "later", daily("r", "02:30")));

            assertThat(resp.occurrences()).isEmpty();
            assertThat(resp.skipped()).hasSize(1);
            assertThat(resp.skipped().get(0).reason()).isEqualTo("GAP_SKIPPED");
            assertThat(resp.skippedCount()).isEqualTo(1);
            assertThat(resp.occurrenceCount()).isZero();
        }

        @Test
        @DisplayName("error aborts with code GAP_ENCOUNTERED")
        void gapError() {
            assertThatThrownBy(() -> service.expand(request(
                    "America/New_York", "2026-03-08", "2026-03-08",
                    "error", "later", daily("r", "02:30"))))
                    .isInstanceOf(LocalTimeResolver.AmbiguousTimeException.class);
        }
    }

    @Nested
    @DisplayName("Fall back overlap")
    class FallOverlap {

        @Test
        @DisplayName("earlier picks the first pass at 05:30Z (-04:00)")
        void overlapEarlier() {
            var resp = service.expand(request(
                    "America/New_York", "2026-11-01", "2026-11-01",
                    "earlier", "earlier", daily("r", "01:30")));

            assertThat(utcList(resp)).containsExactly("2026-11-01T05:30:00Z");
            assertThat(kindList(resp)).containsExactly("OVERLAP_EARLIER");
        }

        @Test
        @DisplayName("later picks the second pass at 06:30Z (-05:00)")
        void overlapLater() {
            var resp = service.expand(request(
                    "America/New_York", "2026-11-01", "2026-11-01",
                    "earlier", "later", daily("r", "01:30")));

            assertThat(utcList(resp)).containsExactly("2026-11-01T06:30:00Z");
            assertThat(kindList(resp)).containsExactly("OVERLAP_LATER");
        }

        @Test
        @DisplayName("skip drops the occurrence and records it as OVERLAP_SKIPPED")
        void overlapSkip() {
            var resp = service.expand(request(
                    "America/New_York", "2026-11-01", "2026-11-01",
                    "earlier", "skip", daily("r", "01:30")));

            assertThat(resp.occurrences()).isEmpty();
            assertThat(resp.skipped().get(0).reason()).isEqualTo("OVERLAP_SKIPPED");
        }

        @Test
        @DisplayName("error aborts for the ambiguous local time")
        void overlapError() {
            assertThatThrownBy(() -> service.expand(request(
                    "America/New_York", "2026-11-01", "2026-11-01",
                    "earlier", "error", daily("r", "01:30"))))
                    .isInstanceOf(LocalTimeResolver.AmbiguousTimeException.class);
        }
    }

    @Test
    @DisplayName("cross-year expansion keeps dates on both sides of January 1st")
    void crossYear() {
        var resp = service.expand(request(
                "Australia/Sydney", "2026-12-31", "2027-01-02",
                "later", "earlier", daily("r", "09:00")));

        assertThat(utcList(resp)).containsExactly(
                "2026-12-30T22:00:00Z",
                "2026-12-31T22:00:00Z",
                "2027-01-01T22:00:00Z");
        assertThat(resp.occurrences()).allSatisfy(o ->
                assertThat(o.offsetSeconds()).isEqualTo(11 * 3600));
    }

    @Test
    @DisplayName("occurrences are sorted by UTC instant across rules regardless of rule order")
    void sortedAcrossRules() {
        var resp = service.expand(request(
                "UTC", "2026-01-01", "2026-01-01", "earlier", "later",
                cron("late", "0 18 * * *"), daily("early", "06:00"), daily("noon", "12:00")));

        assertThat(utcList(resp)).isSorted();
        assertThat(resp.occurrences().stream().map(Occurrence::ruleId).toList())
                .containsExactly("early", "noon", "late");
    }

    @Test
    @DisplayName("sort=false preserves encounter order: rules in request order, then chronological")
    void unsortedKeepsEncounterOrder() {
        ExpandRequest req = request(
                "UTC", "2026-01-01", "2026-01-02", "earlier", "later",
                cron("late", "0 18 * * *"), daily("early", "06:00"));
        req.setSort(false);

        var resp = service.expand(req);
        assertThat(resp.sorted()).isFalse();
        // "late" (18:00 on both days) precedes "early" because its rule comes first in the request.
        assertThat(resp.occurrences().stream().map(Occurrence::ruleId).toList())
                .containsExactly("late", "late", "early", "early");
    }

    @Test
    @DisplayName("identical UTC instants from two rules collapse when deduplicating")
    void deduplicatesSameInstant() {
        var dedup = service.expand(request(
                "UTC", "2026-01-01", "2026-01-01", "earlier", "later",
                daily("a", "12:00"), cron("b", "0 12 * * *")));
        assertThat(dedup.occurrenceCount()).isEqualTo(1);
        assertThat(dedup.duplicateCount()).isEqualTo(1);
        assertThat(dedup.occurrences().get(0).ruleId()).isEqualTo("a");
    }

    @Test
    @DisplayName("identical instants are both retained when deduplicate=false")
    void noDeduplicateKeepsBoth() {
        ExpandRequest req = request("UTC", "2026-01-01", "2026-01-01", "earlier", "later",
                daily("a", "12:00"), cron("b", "0 12 * * *"));
        req.setDeduplicate(false);

        var resp = service.expand(req);
        assertThat(resp.occurrenceCount()).isEqualTo(2);
        assertThat(resp.duplicateCount()).isZero();
        assertThat(resp.deduplicated()).isFalse();
    }

    @Test
    @DisplayName("response carries the TZDB version and relevant gap/overlap transitions")
    void tzdbTraceability() {
        var resp = service.expand(request(
                "America/New_York", "2026-03-01", "2026-11-30",
                "earlier", "later", daily("r", "12:00")));

        assertThat(resp.zoneRules().tzdbVersion()).matches("20\\d{2}[a-z]");
        assertThat(resp.zoneRules().javaVersion()).isNotBlank();
        assertThat(resp.zoneRules().relevantTransitions())
                .extracting(ExpandResponse.TransitionInfo::type)
                .containsExactly("GAP", "OVERLAP");
        var gap = resp.zoneRules().relevantTransitions().get(0);
        assertThat(gap.utcInstant()).isEqualTo("2026-03-08T07:00:00Z");
        assertThat(gap.offsetBeforeSeconds()).isEqualTo(-5 * 3600);
        assertThat(gap.offsetAfterSeconds()).isEqualTo(-4 * 3600);
    }

    @Test
    @DisplayName("candidate and occurrence counts agree for an ordinary range")
    void counts() {
        var resp = service.expand(request(
                "UTC", "2026-01-01", "2026-01-10", "earlier", "later",
                daily("r", "00:00")));
        assertThat(resp.candidateCount()).isEqualTo(10);
        assertThat(resp.occurrenceCount()).isEqualTo(10);
    }

    @Test
    @DisplayName("limit truncates the produced occurrences")
    void limit() {
        ExpandRequest req = request("UTC", "2026-01-01", "2026-01-31", "earlier", "later",
                daily("r", "00:00"));
        req.setLimit(3);
        assertThat(service.expand(req).occurrenceCount()).isEqualTo(3);
    }

    @Test
    @DisplayName("invalid requests fail with stable error codes")
    void validationErrors() {
        ExpandRequest missingZone = request("UTC", "2026-01-01", "2026-01-01",
                "earlier", "later", daily("r", "00:00"));
        missingZone.setZoneId("  ");
        assertThatThrownBy(() -> service.expand(missingZone))
                .isInstanceOf(RequestValidationException.class)
                .extracting(e -> ((RequestValidationException) e).code())
                .isEqualTo("MISSING_ZONE");

        ExpandRequest badZone = request("Not/AZone", "2026-01-01", "2026-01-01",
                "earlier", "later", daily("r", "00:00"));
        assertThatThrownBy(() -> service.expand(badZone))
                .isInstanceOf(RequestValidationException.class)
                .extracting(e -> ((RequestValidationException) e).code())
                .isEqualTo("UNKNOWN_ZONE");

        ExpandRequest badRange = request("UTC", "2026-01-02", "2026-01-01",
                "earlier", "later", daily("r", "00:00"));
        assertThatThrownBy(() -> service.expand(badRange))
                .isInstanceOf(RequestValidationException.class)
                .extracting(e -> ((RequestValidationException) e).code())
                .isEqualTo("INVALID_DATE_RANGE");

        ExpandRequest badPolicy = request("UTC", "2026-01-01", "2026-01-01",
                "nope", "later", daily("r", "00:00"));
        assertThatThrownBy(() -> service.expand(badPolicy))
                .isInstanceOf(RequestValidationException.class)
                .extracting(e -> ((RequestValidationException) e).code())
                .isEqualTo("INVALID_POLICY");

        ExpandRequest badCron = request("UTC", "2026-01-01", "2026-01-01",
                "earlier", "later", cron("r", "not a cron"));
        assertThatThrownBy(() -> service.expand(badCron))
                .isInstanceOf(RequestValidationException.class)
                .extracting(e -> ((RequestValidationException) e).code())
                .isEqualTo("INVALID_RULE");
    }

    @Test
    @DisplayName("a date span beyond the bounded maximum is rejected")
    void dateSpanTooLarge() {
        ExpandRequest req = request("UTC", "2020-01-01", "2099-12-31",
                "earlier", "later", daily("r", "00:00"));
        assertThatThrownBy(() -> service.expand(req))
                .isInstanceOf(RequestValidationException.class)
                .extracting(e -> ((RequestValidationException) e).code())
                .isEqualTo("DATE_RANGE_TOO_LARGE");
    }

    @Test
    @DisplayName("non-positive limit is rejected")
    void nonPositiveLimit() {
        ExpandRequest req = request("UTC", "2026-01-01", "2026-01-02",
                "earlier", "later", daily("r", "00:00"));
        req.setLimit(0);
        assertThatThrownBy(() -> service.expand(req))
                .isInstanceOf(RequestValidationException.class)
                .extracting(e -> ((RequestValidationException) e).code())
                .isEqualTo("INVALID_LIMIT");
    }
}
