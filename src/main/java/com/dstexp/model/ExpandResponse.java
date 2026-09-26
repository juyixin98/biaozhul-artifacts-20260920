package com.dstexp.model;

import com.fasterxml.jackson.annotation.JsonInclude;

import java.util.List;

/**
 * Success response envelope. {@code success} is always {@code true}; {@code occurrences}
 * are sorted and de-duplicated per the request flags.
 */
@JsonInclude(JsonInclude.Include.NON_NULL)
public record ExpandResponse(
        boolean success,
        String zoneId,
        String fromDate,
        String toDate,
        ZoneRulesInfo zoneRules,
        Policies policies,
        int totalRules,
        int candidateCount,
        int occurrenceCount,
        int skippedCount,
        int duplicateCount,
        boolean sorted,
        boolean deduplicated,
        List<Occurrence> occurrences,
        List<SkippedOccurrence> skipped
) {

    @JsonInclude(JsonInclude.Include.NON_NULL)
    public record ZoneRulesInfo(
            String tzdbVersion,
            String javaVersion,
            List<TransitionInfo> relevantTransitions
    ) {
    }

    @JsonInclude(JsonInclude.Include.NON_NULL)
    public record TransitionInfo(
            String utcInstant,
            long offsetBeforeSeconds,
            long offsetAfterSeconds,
            String type
    ) {
    }

    @JsonInclude(JsonInclude.Include.NON_NULL)
    public record Policies(String gapPolicy, String overlapPolicy) {
    }
}
