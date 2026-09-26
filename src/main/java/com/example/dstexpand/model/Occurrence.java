package com.example.dstexpand.model;

import java.time.Instant;
import java.time.LocalDate;
import java.time.LocalDateTime;

/**
 * One expanded occurrence: a daily rule resolved on one date to a UTC instant.
 * Instances are immutable and sorted by (utc, requestedLocal, rule, label).
 */
public final class Occurrence implements Comparable<Occurrence> {

    private final String rule;
    private final String label;
    private final LocalDate date;
    private final LocalDateTime requestedLocal;
    private final LocalDateTime resolvedLocal;
    private final Instant utc;
    private final String resolution;

    public Occurrence(String rule, String label, LocalDate date,
                      LocalDateTime requestedLocal, LocalDateTime resolvedLocal,
                      Instant utc, String resolution) {
        this.rule = rule;
        this.label = label;
        this.date = date;
        this.requestedLocal = requestedLocal;
        this.resolvedLocal = resolvedLocal;
        this.utc = utc;
        this.resolution = resolution;
    }

    /** Rule wall-clock time, e.g. "02:30". */
    public String getRule() {
        return rule;
    }

    public String getLabel() {
        return label;
    }

    public LocalDate getDate() {
        return date;
    }

    /** Local date-time the rule asked for (may not exist / exist twice). */
    public LocalDateTime getRequestedLocal() {
        return requestedLocal;
    }

    /** Local date-time actually displayed at the resolved UTC instant. */
    public LocalDateTime getResolvedLocal() {
        return resolvedLocal;
    }

    /** Resolved UTC instant, ISO-8601, e.g. "2026-03-08T07:30:00Z". */
    public Instant getUtc() {
        return utc;
    }

    /** NORMAL, GAP_EARLIER, GAP_LATER, OVERLAP_EARLIER or OVERLAP_LATER. */
    public String getResolution() {
        return resolution;
    }

    @Override
    public int compareTo(Occurrence other) {
        int c = utc.compareTo(other.utc);
        if (c != 0) {
            return c;
        }
        c = requestedLocal.compareTo(other.requestedLocal);
        if (c != 0) {
            return c;
        }
        c = rule.compareTo(other.rule);
        if (c != 0) {
            return c;
        }
        String a = label == null ? "" : label;
        String b = other.label == null ? "" : other.label;
        return a.compareTo(b);
    }
}
