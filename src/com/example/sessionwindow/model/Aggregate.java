package com.example.sessionwindow.model;

import java.util.LinkedHashMap;
import java.util.Map;

/**
 * Immutable aggregate of the events inside one session window.
 *
 * @param start session start = minimum event timestamp (inclusive)
 * @param end   session end   = maximum event timestamp + session gap (exclusive);
 *              a later event at exactly {@code end} opens a new session
 * @param count number of events
 * @param sum   sum of event values
 * @param min   minimum event value
 * @param max   maximum event value
 */
public record Aggregate(long start, long end, long count, double sum, double min, double max) {

    public static Aggregate single(long timestamp, double value, long gap) {
        return new Aggregate(timestamp, timestamp + gap, 1, value, value, value);
    }

    /** Merge two aggregates belonging to the same key (in any order). */
    public Aggregate merge(Aggregate other) {
        return new Aggregate(
                Math.min(this.start, other.start),
                Math.max(this.end, other.end),
                this.count + other.count,
                this.sum + other.sum,
                Math.min(this.min, other.min),
                Math.max(this.max, other.max));
    }

    public double avg() {
        return count == 0 ? 0.0 : sum / count;
    }

    public Map<String, Object> toJson() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("start", start);
        m.put("end", end);
        m.put("count", count);
        m.put("sum", sum);
        m.put("min", min);
        m.put("max", max);
        m.put("avg", avg());
        return m;
    }

    @Override
    public String toString() {
        return "Aggregate[start=" + start + ", end=" + end + ", count=" + count
                + ", sum=" + sum + ", avg=" + avg() + "]";
    }
}
