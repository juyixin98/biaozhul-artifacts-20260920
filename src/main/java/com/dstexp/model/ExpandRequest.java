package com.dstexp.model;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;

import java.util.List;

/**
 * JSON request body. Example:
 * <pre>
 * {
 *   "zoneId": "America/New_York",
 *   "fromDate": "2026-03-01",
 *   "toDate":   "2026-11-30",
 *   "rules": [ { "ruleId": "standup", "type": "daily", "localTime": "02:30" } ],
 *   "gapPolicy": "earlier",
 *   "overlapPolicy": "later",
 *   "sort": true,
 *   "deduplicate": true
 * }
 * </pre>
 */
@JsonIgnoreProperties(ignoreUnknown = true)
public class ExpandRequest {

    /** IANA time-zone id, e.g. {@code Europe/Berlin}. Required. */
    private String zoneId;

    /** Inclusive first local date, ISO {@code yyyy-MM-dd}. Required. */
    private String fromDate;

    /** Inclusive last local date, ISO {@code yyyy-MM-dd}. Required. */
    private String toDate;

    /** One or more rules to expand. Required, non-empty. */
    private List<RuleSpec> rules;

    /** earlier | later | skip | error. Defaults to {@code earlier}. */
    private String gapPolicy;

    /** earlier | later | skip | error. Defaults to {@code later}. */
    private String overlapPolicy;

    /** Whether occurrences are sorted by UTC instant. Defaults to {@code true}. */
    private Boolean sort;

    /** Whether identical (instant) occurrences are merged. Defaults to {@code true}. */
    private Boolean deduplicate;

    /** Optional cap on the number of returned occurrences. */
    private Integer limit;

    public String getZoneId() { return zoneId; }
    public void setZoneId(String zoneId) { this.zoneId = zoneId; }

    public String getFromDate() { return fromDate; }
    public void setFromDate(String fromDate) { this.fromDate = fromDate; }

    public String getToDate() { return toDate; }
    public void setToDate(String toDate) { this.toDate = toDate; }

    public List<RuleSpec> getRules() { return rules; }
    public void setRules(List<RuleSpec> rules) { this.rules = rules; }

    public String getGapPolicy() { return gapPolicy; }
    public void setGapPolicy(String gapPolicy) { this.gapPolicy = gapPolicy; }

    public String getOverlapPolicy() { return overlapPolicy; }
    public void setOverlapPolicy(String overlapPolicy) { this.overlapPolicy = overlapPolicy; }

    public Boolean getSort() { return sort; }
    public void setSort(Boolean sort) { this.sort = sort; }

    public Boolean getDeduplicate() { return deduplicate; }
    public void setDeduplicate(Boolean deduplicate) { this.deduplicate = deduplicate; }

    public Integer getLimit() { return limit; }
    public void setLimit(Integer limit) { this.limit = limit; }
}
