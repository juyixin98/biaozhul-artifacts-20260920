package com.example.intervals.json;

import com.fasterxml.jackson.annotation.JsonInclude;
import com.fasterxml.jackson.annotation.JsonProperty;

@JsonInclude(JsonInclude.Include.ALWAYS)
public record ErrorDto(
        @JsonProperty("code") String code,
        @JsonProperty("message") String message) {
}
