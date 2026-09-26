package com.example.dstexpand.model;

import java.util.List;

/**
 * Expansion response (JSON out). Either {@code occurrences} are present, or
 * the whole expansion failed and {@link #getError()} is non-null.
 */
public final class ExpandResponse {

    private final String zoneId;
    private final String tzdbVersion;
    private final String startDate;
    private final String endDate;
    private final String gapPolicy;
    private final String overlapPolicy;
    private final Stats stats;
    private final List<Occurrence> occurrences;
    private final List<SkippedItem> skipped;
    private final String error;

    private ExpandResponse(Builder b) {
        this.zoneId = b.zoneId;
        this.tzdbVersion = b.tzdbVersion;
        this.startDate = b.startDate;
        this.endDate = b.endDate;
        this.gapPolicy = b.gapPolicy;
        this.overlapPolicy = b.overlapPolicy;
        this.stats = b.stats;
        this.occurrences = b.occurrences;
        this.skipped = b.skipped;
        this.error = b.error;
    }

    public String getZoneId() {
        return zoneId;
    }

    /** IANA tz database version backing the JVM, e.g. "2024a". */
    public String getTzdbVersion() {
        return tzdbVersion;
    }

    public String getStartDate() {
        return startDate;
    }

    public String getEndDate() {
        return endDate;
    }

    public String getGapPolicy() {
        return gapPolicy;
    }

    public String getOverlapPolicy() {
        return overlapPolicy;
    }

    public Stats getStats() {
        return stats;
    }

    /** Expanded instants, sorted by UTC ascending, duplicates removed. */
    public List<Occurrence> getOccurrences() {
        return occurrences;
    }

    /** Rule/date combinations dropped by a SKIP policy. */
    public List<SkippedItem> getSkipped() {
        return skipped;
    }

    /** Non-null when the expansion failed (e.g. policy ERROR was hit). */
    public String getError() {
        return error;
    }

    public static Builder success() {
        return new Builder();
    }

    public static ExpandResponse failure(String message) {
        Builder b = new Builder();
        b.error = message;
        return b.build();
    }

    /** Counters describing the expansion, for traceability and tests. */
    public static final class Stats {
        private final int inputRules;
        private final int uniqueRules;
        private final int duplicateRulesRemoved;
        private final long days;
        private final int occurrences;
        private final int duplicatesRemoved;
        private final int skipped;

        public Stats(int inputRules, int uniqueRules, int duplicateRulesRemoved, long days,
                     int occurrences, int duplicatesRemoved, int skipped) {
            this.inputRules = inputRules;
            this.uniqueRules = uniqueRules;
            this.duplicateRulesRemoved = duplicateRulesRemoved;
            this.days = days;
            this.occurrences = occurrences;
            this.duplicatesRemoved = duplicatesRemoved;
            this.skipped = skipped;
        }

        public int getInputRules() {
            return inputRules;
        }

        public int getUniqueRules() {
            return uniqueRules;
        }

        public int getDuplicateRulesRemoved() {
            return duplicateRulesRemoved;
        }

        public long getDays() {
            return days;
        }

        public int getOccurrences() {
            return occurrences;
        }

        /** Identical (utc, rule, label) rows removed after expansion. */
        public int getDuplicatesRemoved() {
            return duplicatesRemoved;
        }

        public int getSkipped() {
            return skipped;
        }
    }

    public static final class Builder {
        private String zoneId;
        private String tzdbVersion;
        private String startDate;
        private String endDate;
        private String gapPolicy;
        private String overlapPolicy;
        private Stats stats;
        private List<Occurrence> occurrences = List.of();
        private List<SkippedItem> skipped = List.of();
        private String error;

        public Builder zoneId(String v) {
            this.zoneId = v;
            return this;
        }

        public Builder tzdbVersion(String v) {
            this.tzdbVersion = v;
            return this;
        }

        public Builder startDate(String v) {
            this.startDate = v;
            return this;
        }

        public Builder endDate(String v) {
            this.endDate = v;
            return this;
        }

        public Builder gapPolicy(String v) {
            this.gapPolicy = v;
            return this;
        }

        public Builder overlapPolicy(String v) {
            this.overlapPolicy = v;
            return this;
        }

        public Builder stats(Stats v) {
            this.stats = v;
            return this;
        }

        public Builder occurrences(List<Occurrence> v) {
            this.occurrences = v;
            return this;
        }

        public Builder skipped(List<SkippedItem> v) {
            this.skipped = v;
            return this;
        }

        public ExpandResponse build() {
            return new ExpandResponse(this);
        }
    }
}
