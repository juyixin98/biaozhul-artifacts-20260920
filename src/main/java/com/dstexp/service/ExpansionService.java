package com.dstexp.service;

import com.dstexp.model.ExpandRequest;
import com.dstexp.model.ExpandResponse;
import com.dstexp.model.GapStrategy;
import com.dstexp.model.Occurrence;
import com.dstexp.model.OverlapStrategy;
import com.dstexp.model.RuleSpec;
import com.dstexp.model.SkippedOccurrence;
import com.dstexp.schedule.LocalTimeResolver;
import com.dstexp.schedule.RulePlanner;

import java.time.DateTimeException;
import java.time.Instant;
import java.time.LocalDate;
import java.time.LocalDateTime;
import java.time.ZoneId;
import java.time.ZoneOffset;
import java.time.format.DateTimeParseException;
import java.time.zone.ZoneOffsetTransition;
import java.time.zone.ZoneRules;
import java.time.zone.ZoneRulesException;
import java.time.zone.ZoneRulesProvider;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashSet;
import java.util.List;
import java.util.NavigableMap;
import java.util.Set;

/**
 * Orchestrates request validation, local-time planning, gap/overlap resolution, sorting
 * and de-duplication. Pure computation, no I/O.
 */
public final class ExpansionService {

    private static final int DEFAULT_LIMIT = 100_000;
    private static final int MAX_LIMIT = 1_000_000;
    /** Upper bound on a single requested date span, to keep expansions bounded. */
    private static final long MAX_DATE_SPAN_DAYS = 366 * 50;

    /** Internal working item carrying the parsed instant for cheap sorting. */
    private record PlannedOccurrence(Instant instant, String ruleId, LocalDateTime local,
                                    ZoneOffset offset, LocalTimeResolver.Kind kind) {

        Occurrence toOccurrence() {
            return new Occurrence(
                    instant.toString(), ruleId, local.toString(),
                    offset.getTotalSeconds(), kind.name());
        }
    }

    public ExpandResponse expand(ExpandRequest request) {
        validateShape(request);

        ZoneId zone = parseZone(request.getZoneId());
        LocalDate from = parseDate(request.getFromDate(), "fromDate");
        LocalDate to = parseDate(request.getToDate(), "toDate");
        if (from.isAfter(to)) {
            throw new RequestValidationException("INVALID_DATE_RANGE",
                    "fromDate " + from + " must not be after toDate " + to);
        }
        if (java.time.temporal.ChronoUnit.DAYS.between(from, to) > MAX_DATE_SPAN_DAYS) {
            throw new RequestValidationException("DATE_RANGE_TOO_LARGE",
                    "Date span must not exceed " + MAX_DATE_SPAN_DAYS + " days");
        }

        GapStrategy gap;
        OverlapStrategy overlap;
        try {
            gap = GapStrategy.fromJson(request.getGapPolicy());
            overlap = OverlapStrategy.fromJson(request.getOverlapPolicy());
        } catch (IllegalArgumentException e) {
            throw new RequestValidationException("INVALID_POLICY", e.getMessage());
        }
        boolean shouldSort = request.getSort() == null || request.getSort();
        boolean shouldDeduplicate = request.getDeduplicate() == null || request.getDeduplicate();
        int limit = resolveLimit(request.getLimit());

        ZoneRules rules = zone.getRules();
        List<PlannedOccurrence> planned = new ArrayList<>();
        List<SkippedOccurrence> skipped = new ArrayList<>();
        int candidateCount = 0;

        for (RuleSpec spec : request.getRules()) {
            validateRule(spec);
            List<LocalDateTime> locals;
            try {
                locals = RulePlanner.plan(spec, from, to);
            } catch (IllegalArgumentException e) {
                throw new RequestValidationException("INVALID_RULE", e.getMessage());
            }
            candidateCount += locals.size();
            for (LocalDateTime local : locals) {
                if (planned.size() >= limit) {
                    break;
                }
                var resolved = LocalTimeResolver.resolve(local, rules, gap, overlap).orElse(null);
                if (resolved == null) {
                    String reason = rules.getValidOffsets(local).isEmpty()
                            ? "GAP_SKIPPED" : "OVERLAP_SKIPPED";
                    skipped.add(new SkippedOccurrence(spec.getRuleId(), local.toString(), reason));
                } else {
                    planned.add(new PlannedOccurrence(
                            resolved.instant(), spec.getRuleId(), local,
                            resolved.offset(), resolved.kind()));
                }
            }
        }

        int duplicateCount = 0;
        if (shouldDeduplicate) {
            DedupResult deduped = deduplicate(planned);
            planned = deduped.kept;
            duplicateCount = deduped.removed;
        }

        if (shouldSort) {
            planned.sort(Comparator.comparing(PlannedOccurrence::instant)
                    .thenComparing(PlannedOccurrence::ruleId,
                            Comparator.nullsLast(Comparator.naturalOrder()))
                    .thenComparing(PlannedOccurrence::local));
            skipped.sort(Comparator.comparing(SkippedOccurrence::localDateTime)
                    .thenComparing(SkippedOccurrence::ruleId));
        }

        List<Occurrence> occurrences = planned.stream()
                .map(PlannedOccurrence::toOccurrence)
                .toList();
        ExpandResponse.ZoneRulesInfo zoneInfo = new ExpandResponse.ZoneRulesInfo(
                tzdbVersion(zone), System.getProperty("java.version"),
                relevantTransitions(rules, from, to));

        return new ExpandResponse(
                true, zone.getId(), from.toString(), to.toString(), zoneInfo,
                new ExpandResponse.Policies(gap.name().toLowerCase(), overlap.name().toLowerCase()),
                request.getRules().size(), candidateCount, occurrences.size(),
                skipped.size(), duplicateCount, shouldSort, shouldDeduplicate,
                occurrences, List.copyOf(skipped));
    }

    private record DedupResult(List<PlannedOccurrence> kept, int removed) {
    }

    /**
     * Keeps the first occurrence per UTC instant in encounter (rule, then chronological)
     * order. The retained identity therefore follows rule order before sorting.
     */
    private static DedupResult deduplicate(List<PlannedOccurrence> planned) {
        Set<Instant> seen = new HashSet<>();
        List<PlannedOccurrence> kept = new ArrayList<>();
        int removed = 0;
        for (PlannedOccurrence p : planned) {
            if (seen.add(p.instant())) {
                kept.add(p);
            } else {
                removed++;
            }
        }
        return new DedupResult(kept, removed);
    }

    private static int resolveLimit(Integer requested) {
        if (requested == null) {
            return DEFAULT_LIMIT;
        }
        if (requested <= 0) {
            throw new RequestValidationException("INVALID_LIMIT", "limit must be positive");
        }
        return Math.min(requested, MAX_LIMIT);
    }

    private static void validateShape(ExpandRequest request) {
        if (request == null) {
            throw new RequestValidationException("EMPTY_REQUEST", "Request body must be a JSON object");
        }
        if (request.getRules() == null || request.getRules().isEmpty()) {
            throw new RequestValidationException("NO_RULES", "At least one rule is required");
        }
        if (request.getZoneId() == null || request.getZoneId().isBlank()) {
            throw new RequestValidationException("MISSING_ZONE", "zoneId is required");
        }
        if (request.getFromDate() == null || request.getToDate() == null) {
            throw new RequestValidationException("MISSING_DATE_RANGE",
                    "fromDate and toDate are required");
        }
        Set<String> ids = new HashSet<>();
        for (RuleSpec spec : request.getRules()) {
            if (spec.getRuleId() == null || spec.getRuleId().isBlank()) {
                throw new RequestValidationException("MISSING_RULE_ID",
                        "Every rule needs a non-empty ruleId");
            }
            if (!ids.add(spec.getRuleId())) {
                throw new RequestValidationException("DUPLICATE_RULE_ID",
                        "Duplicate ruleId: " + spec.getRuleId());
            }
        }
    }

    private static void validateRule(RuleSpec spec) {
        String type = spec.getType() == null ? "" : spec.getType().trim().toLowerCase();
        switch (type) {
            case "daily" -> {
                if (spec.getLocalTime() == null || spec.getLocalTime().isBlank()) {
                    throw new RequestValidationException("MISSING_LOCAL_TIME",
                            "Daily rule '" + spec.getRuleId() + "' requires localTime");
                }
            }
            case "cron" -> {
                if (spec.getCron() == null || spec.getCron().isBlank()) {
                    throw new RequestValidationException("MISSING_CRON",
                            "Cron rule '" + spec.getRuleId() + "' requires cron");
                }
            }
            default -> throw new RequestValidationException("UNSUPPORTED_RULE_TYPE",
                    "Rule '" + spec.getRuleId() + "' has unsupported type '" + spec.getType()
                            + "' (allowed: daily, cron)");
        }
    }

    private static ZoneId parseZone(String raw) {
        try {
            return ZoneId.of(raw.trim());
        } catch (DateTimeException e) {
            throw new RequestValidationException("UNKNOWN_ZONE",
                    "Unknown IANA time zone id: " + raw);
        }
    }

    private static LocalDate parseDate(String raw, String field) {
        try {
            return LocalDate.parse(raw);
        } catch (DateTimeParseException | NullPointerException e) {
            throw new RequestValidationException("INVALID_DATE",
                    field + " must be ISO yyyy-MM-dd: " + raw);
        }
    }

    private static String tzdbVersion(ZoneId zone) {
        try {
            NavigableMap<String, ZoneRules> versions = ZoneRulesProvider.getVersions(zone.getId());
            return versions.lastKey();
        } catch (ZoneRulesException e) {
            // Offset-based ids such as "+02:00" are not backed by a versioned TZDB group.
            return "fixed-offset:" + zone.getId();
        }
    }

    /**
     * Collects gap/overlap transitions whose local date-time lies inside the requested range,
     * using both the zone's explicit historical transitions and its recurring transition rules
     * (which govern future years such as 2026).
     */
    private static List<ExpandResponse.TransitionInfo> relevantTransitions(
            ZoneRules rules, LocalDate from, LocalDate to) {
        LocalDateTime windowStart = from.atStartOfDay();
        LocalDateTime windowEnd = to.atTime(23, 59, 59);

        List<ZoneOffsetTransition> all = new ArrayList<>(rules.getTransitions());
        for (int year = from.getYear(); year <= to.getYear(); year++) {
            for (var rule : rules.getTransitionRules()) {
                ZoneOffsetTransition t = rule.createTransition(year);
                if (t != null) {
                    all.add(t);
                }
            }
        }

        List<ExpandResponse.TransitionInfo> out = new ArrayList<>();
        Set<String> seen = new HashSet<>();
        all.stream()
                .filter(t -> !t.getDateTimeBefore().isBefore(windowStart)
                        && !t.getDateTimeBefore().isAfter(windowEnd))
                .distinct()
                .sorted(Comparator.comparing(ZoneOffsetTransition::getInstant))
                .forEach(t -> {
                    if (seen.add(t.getInstant().toString())) {
                        out.add(new ExpandResponse.TransitionInfo(
                                t.getInstant().toString(),
                                t.getOffsetBefore().getTotalSeconds(),
                                t.getOffsetAfter().getTotalSeconds(),
                                t.isGap() ? "GAP" : "OVERLAP"));
                    }
                });
        return out;
    }
}
