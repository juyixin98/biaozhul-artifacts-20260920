package com.example.dstexpand.model;

import com.fasterxml.jackson.annotation.JsonCreator;
import com.fasterxml.jackson.annotation.JsonProperty;

import java.util.List;

/**
 * Expansion request (JSON in).
 *
 * <pre>{
 *   "zoneId": "America/New_York",
 *   "startDate": "2026-03-07",
 *   "endDate": "2026-03-09",
 *   "rules": [ {"time": "02:30", "label": "nightly"} ],
 *   "gapPolicy": "LATER",
 *   "overlapPolicy": "EARLIER"
 * }</pre>
 *
 * gapPolicy/overlapPolicy are optional; defaults are LATER for gaps and
 * EARLIER for overlaps (see README).
 */
public final class ExpandRequest {

    private final String zoneId;
    private final String startDate;
    private final String endDate;
    private final List<DailyRule> rules;
    private final ResolutionPolicy gapPolicy;
    private final ResolutionPolicy overlapPolicy;

    @JsonCreator
    public ExpandRequest(@JsonProperty("zoneId") String zoneId,
                         @JsonProperty("startDate") String startDate,
                         @JsonProperty("endDate") String endDate,
                         @JsonProperty("rules") List<DailyRule> rules,
                         @JsonProperty("gapPolicy") ResolutionPolicy gapPolicy,
                         @JsonProperty("overlapPolicy") ResolutionPolicy overlapPolicy) {
        this.zoneId = zoneId;
        this.startDate = startDate;
        this.endDate = endDate;
        this.rules = rules;
        this.gapPolicy = gapPolicy;
        this.overlapPolicy = overlapPolicy;
    }

    public String getZoneId() {
        return zoneId;
    }

    public String getStartDate() {
        return startDate;
    }

    public String getEndDate() {
        return endDate;
    }

    public List<DailyRule> getRules() {
        return rules;
    }

    /** Policy for non-existent local times; defaults to LATER. */
    public ResolutionPolicy getGapPolicy() {
        return gapPolicy == null ? ResolutionPolicy.LATER : gapPolicy;
    }

    /** Policy for ambiguous local times; defaults to EARLIER. */
    public ResolutionPolicy getOverlapPolicy() {
        return overlapPolicy == null ? ResolutionPolicy.EARLIER : overlapPolicy;
    }
}
