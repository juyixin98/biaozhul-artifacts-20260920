package com.example.dstexpand.model;

import com.fasterxml.jackson.annotation.JsonCreator;
import com.fasterxml.jackson.annotation.JsonProperty;

import java.util.Objects;

/**
 * A single daily local-time rule: every day in the requested range, at this
 * wall-clock time in the requested zone, produce one UTC instant.
 */
public final class DailyRule {

    private final String time;
    private final String label;

    @JsonCreator
    public DailyRule(@JsonProperty("time") String time,
                     @JsonProperty("label") String label) {
        this.time = time;
        this.label = label;
    }

    /** Local wall-clock time in {@code HH:mm} (24-hour) form. */
    public String getTime() {
        return time;
    }

    /** Optional human-readable label carried through to the output. */
    public String getLabel() {
        return label;
    }

    /** Identity used for de-duplication: time + label. */
    public String dedupKey() {
        return time + "|" + (label == null ? "" : label);
    }

    @Override
    public boolean equals(Object o) {
        if (this == o) {
            return true;
        }
        if (!(o instanceof DailyRule other)) {
            return false;
        }
        return Objects.equals(time, other.time) && Objects.equals(label, other.label);
    }

    @Override
    public int hashCode() {
        return Objects.hash(time, label);
    }
}
