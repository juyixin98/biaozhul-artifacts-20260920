package com.example.intervals.json;

import com.fasterxml.jackson.annotation.JsonInclude;
import com.fasterxml.jackson.annotation.JsonProperty;

import java.util.List;

/**
 * The normalized result of an expression: its disjoint sorted intervals plus
 * simple cardinality metadata.
 */
@JsonInclude(JsonInclude.Include.ALWAYS)
public record ResultDto(
        @JsonProperty("intervals") List<IntervalDto> intervals,
        @JsonProperty("intervalCount") int intervalCount,
        @JsonProperty("empty") boolean empty) {
}
