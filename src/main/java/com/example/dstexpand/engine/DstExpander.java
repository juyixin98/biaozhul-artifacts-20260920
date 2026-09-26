package com.example.dstexpand.engine;

import com.example.dstexpand.model.DailyRule;
import com.example.dstexpand.model.ExpandRequest;
import com.example.dstexpand.model.ExpandResponse;
import com.example.dstexpand.model.Occurrence;
import com.example.dstexpand.model.ResolutionPolicy;
import com.example.dstexpand.model.SkippedItem;
import com.example.dstexpand.tz.TzdbVersion;

import java.time.DateTimeException;
import java.time.Instant;
import java.time.LocalDate;
import java.time.LocalDateTime;
import java.time.LocalTime;
import java.time.ZoneId;
import java.time.temporal.ChronoUnit;
import java.time.zone.ZoneOffsetTransition;
import java.time.zone.ZoneRules;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeSet;

/**
 * Expands daily local-time rules into UTC instants within one IANA time zone.
 *
 * <p>For every date in [startDate, endDate] and every rule, the local
 * date-time is classified with {@link ZoneRules#getValidOffsets}:</p>
 * <ul>
 *   <li>exactly one offset: normal day, direct conversion;</li>
 *   <li>no offset: the local time does not exist (spring-forward gap),
 *       resolved per {@code gapPolicy};</li>
 *   <li>two offsets: the local time occurs twice (fall-back overlap),
 *       resolved per {@code overlapPolicy}.</li>
 * </ul>
 *
 * <p>Output occurrences are de-duplicated and sorted by UTC ascending.</p>
 */
public final class DstExpander {

    /** Safety bound on the expansion window (~10 years). */
    static final long MAX_DAYS = 3662;

    public ExpandResponse expand(ExpandRequest request) {
        Validated validated = validate(request);
        List<Occurrence> occurrences = new ArrayList<>();
        List<SkippedItem> skipped = new ArrayList<>();

        for (LocalDate date = validated.start; !date.isAfter(validated.end); date = date.plusDays(1)) {
            for (DailyRule rule : validated.rules) {
                resolveOne(validated, rule, date, occurrences, skipped);
            }
        }

        TreeSet<Occurrence> deduped = new TreeSet<>(occurrences);
        List<Occurrence> sorted = List.copyOf(deduped);
        List<SkippedItem> skippedOut = List.copyOf(skipped);

        ExpandResponse.Stats stats = new ExpandResponse.Stats(
                request.getRules().size(),
                validated.rules.size(),
                request.getRules().size() - validated.rules.size(),
                validated.days,
                sorted.size(),
                occurrences.size() - sorted.size(),
                skippedOut.size());

        return ExpandResponse.success()
                .zoneId(validated.zone.getId())
                .tzdbVersion(TzdbVersion.current())
                .startDate(validated.start.toString())
                .endDate(validated.end.toString())
                .gapPolicy(validated.gapPolicy.name())
                .overlapPolicy(validated.overlapPolicy.name())
                .stats(stats)
                .occurrences(sorted)
                .skipped(skippedOut)
                .build();
    }

    private void resolveOne(Validated validated, DailyRule rule, LocalDate date,
                            List<Occurrence> occurrences, List<SkippedItem> skipped) {
        LocalTime time = parseTime(rule.getTime());
        LocalDateTime local = LocalDateTime.of(date, time);
        ZoneRules rules = validated.zone.getRules();
        List<ZoneOffset> offsets = rules.getValidOffsets(local);

        if (offsets.size() == 1) {
            add(occurrences, validated, rule, date, local, local.atOffset(offsets.get(0)).toInstant(), "NORMAL");
        } else if (offsets.isEmpty()) {
            resolveGap(validated, rule, date, local, rules.getTransition(local), occurrences, skipped);
        } else {
            resolveOverlap(validated, rule, date, local, offsets, occurrences, skipped);
        }
    }

    private void resolveGap(Validated validated, DailyRule rule, LocalDate date, LocalDateTime local,
                            ZoneOffsetTransition transition, List<Occurrence> occurrences,
                            List<SkippedItem> skipped) {
        switch (validated.gapPolicy) {
            case EARLIER ->
                // Earlier UTC instant: interpret with the post-transition offset.
                add(occurrences, validated, rule, date, local,
                        local.atOffset(transition.getOffsetAfter()).toInstant(), "GAP_EARLIER");
            case LATER ->
                // Later UTC instant: interpret with the pre-transition offset.
                add(occurrences, validated, rule, date, local,
                        local.atOffset(transition.getOffsetBefore()).toInstant(), "GAP_LATER");
            case SKIP ->
                skipped.add(new SkippedItem(rule.getTime(), rule.getLabel(), date,
                        local.toString(), "GAP_SKIPPED"));
            case ERROR ->
                throw new ExpansionException("Local time " + local + " does not exist in zone "
                        + validated.zone.getId() + " (DST gap, transition at "
                        + transition.getDateTimeBefore() + "); gapPolicy=ERROR");
        }
    }

    private void resolveOverlap(Validated validated, DailyRule rule, LocalDate date, LocalDateTime local,
                                List<ZoneOffset> offsets, List<Occurrence> occurrences,
                                List<SkippedItem> skipped) {
        switch (validated.overlapPolicy) {
            case EARLIER ->
                // getValidOffsets returns [offsetBefore, offsetAfter]; the larger
                // (pre-transition) offset yields the earlier UTC instant.
                add(occurrences, validated, rule, date, local,
                        local.atOffset(offsets.get(0)).toInstant(), "OVERLAP_EARLIER");
            case LATER ->
                add(occurrences, validated, rule, date, local,
                        local.atOffset(offsets.get(1)).toInstant(), "OVERLAP_LATER");
            case SKIP ->
                skipped.add(new SkippedItem(rule.getTime(), rule.getLabel(), date,
                        local.toString(), "OVERLAP_SKIPPED"));
            case ERROR ->
                throw new ExpansionException("Local time " + local + " is ambiguous in zone "
                        + validated.zone.getId() + " (DST overlap); overlapPolicy=ERROR");
        }
    }

    private void add(List<Occurrence> out, Validated validated, DailyRule rule, LocalDate date,
                     LocalDateTime requested, Instant utc, String resolution) {
        LocalDateTime resolved = LocalDateTime.ofInstant(utc, validated.zone);
        out.add(new Occurrence(rule.getTime(), rule.getLabel(), date, requested, resolved, utc, resolution));
    }

    private Validated validate(ExpandRequest request) {
        if (request == null) {
            throw new ExpansionException("Request body must be a JSON object");
        }
        ZoneId zone = parseZone(request.getZoneId());
        LocalDate start = parseDate("startDate", request.getStartDate());
        LocalDate end = parseDate("endDate", request.getEndDate());
        if (start.isAfter(end)) {
            throw new ExpansionException("startDate " + start + " is after endDate " + end);
        }
        long days = ChronoUnit.DAYS.between(start, end) + 1;
        if (days > MAX_DAYS) {
            throw new ExpansionException("Date range spans " + days + " days, maximum is " + MAX_DAYS);
        }
        if (request.getRules() == null || request.getRules().isEmpty()) {
            throw new ExpansionException("rules must contain at least one daily rule");
        }
        Map<String, DailyRule> unique = new LinkedHashMap<>();
        for (DailyRule rule : request.getRules()) {
            if (rule == null || rule.getTime() == null) {
                throw new ExpansionException("Every rule needs a \"time\" field (HH:mm)");
            }
            parseTime(rule.getTime());
            unique.putIfAbsent(rule.dedupKey(), rule);
        }
        return new Validated(zone, start, end, days, List.copyOf(unique.values()),
                request.getGapPolicy(), request.getOverlapPolicy());
    }

    private static ZoneId parseZone(String zoneId) {
        if (zoneId == null || zoneId.isBlank()) {
            throw new ExpansionException("zoneId is required (IANA name, e.g. \"America/New_York\")");
        }
        try {
            return ZoneId.of(zoneId);
        } catch (DateTimeException e) {
            throw new ExpansionException("Unknown zoneId: " + zoneId);
        }
    }

    private static LocalDate parseDate(String field, String value) {
        if (value == null) {
            throw new ExpansionException(field + " is required (ISO date, e.g. \"2026-03-08\")");
        }
        try {
            return LocalDate.parse(value);
        } catch (DateTimeException e) {
            throw new ExpansionException(field + " is not a valid ISO date: " + value);
        }
    }

    private static LocalTime parseTime(String value) {
        try {
            return LocalTime.parse(value);
        } catch (DateTimeException e) {
            throw new ExpansionException("Rule time is not a valid HH:mm value: " + value);
        }
    }

    /** Immutable, fully-validated view of a request. */
    private record Validated(ZoneId zone, LocalDate start, LocalDate end, long days,
                             List<DailyRule> rules, ResolutionPolicy gapPolicy,
                             ResolutionPolicy overlapPolicy) {
    }
}
