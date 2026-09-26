package com.example.intervals.fixed;

import com.example.intervals.json.IntervalDto;
import com.fasterxml.jackson.annotation.JsonInclude;
import com.fasterxml.jackson.annotation.JsonProperty;

import java.util.List;
import java.util.Map;

/**
 * One named fixed dataset loaded from {@code data/datasets.json}.
 */
@JsonInclude(JsonInclude.Include.NON_NULL)
public record DatasetDto(
        @JsonProperty("id") String id,
        @JsonProperty("domain") String domain,
        @JsonProperty("description") String description,
        @JsonProperty("sets") Map<String, List<IntervalDto>> sets) {
}
