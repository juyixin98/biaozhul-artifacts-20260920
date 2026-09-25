package com.eventorder.model;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;

/** Raw event as deserialized from the request JSON. Immutable after parsing. */
@JsonIgnoreProperties(ignoreUnknown = true)
public record EventInput(
        String id,
        IntervalInput interval,
        VersionInput version) {

    @JsonIgnoreProperties(ignoreUnknown = true)
    public record IntervalInput(String start, String end) {
    }

    @JsonIgnoreProperties(ignoreUnknown = true)
    public record VersionInput(String stream, long value) {
    }
}
