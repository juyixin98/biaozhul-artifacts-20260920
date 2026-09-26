package com.example.intervals.json;

import com.fasterxml.jackson.annotation.JsonInclude;
import com.fasterxml.jackson.annotation.JsonProperty;

/**
 * JSON form of one interval.
 *
 * <pre>
 * { "lower": "2024-01-01T00:00:00Z", "lowerOpen": false,
 *   "upper": "2024-06-30T23:59:59Z", "upperOpen": true }
 * </pre>
 *
 * A null or missing {@code lower}/{@code upper} denotes the corresponding
 * infinite end. Open flags default to false (closed) when absent.
 */
@JsonInclude(JsonInclude.Include.ALWAYS)
public record IntervalDto(
        @JsonProperty("lower") String lower,
        @JsonProperty("lowerOpen") Boolean lowerOpen,
        @JsonProperty("upper") String upper,
        @JsonProperty("upperOpen") Boolean upperOpen) {

    public boolean lowerIsOpen() {
        return Boolean.TRUE.equals(lowerOpen);
    }

    public boolean upperIsOpen() {
        return Boolean.TRUE.equals(upperOpen);
    }
}
