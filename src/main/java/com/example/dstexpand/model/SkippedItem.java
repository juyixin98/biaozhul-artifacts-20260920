package com.example.dstexpand.model;

import java.time.LocalDate;

/**
 * A rule/date combination that was dropped because the policy was SKIP.
 */
public final class SkippedItem {

    private final String rule;
    private final String label;
    private final LocalDate date;
    private final String localTime;
    private final String reason;

    public SkippedItem(String rule, String label, LocalDate date, String localTime, String reason) {
        this.rule = rule;
        this.label = label;
        this.date = date;
        this.localTime = localTime;
        this.reason = reason;
    }

    public String getRule() {
        return rule;
    }

    public String getLabel() {
        return label;
    }

    public LocalDate getDate() {
        return date;
    }

    public String getLocalTime() {
        return localTime;
    }

    /** GAP_SKIPPED or OVERLAP_SKIPPED. */
    public String getReason() {
        return reason;
    }
}
