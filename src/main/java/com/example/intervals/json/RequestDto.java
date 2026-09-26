package com.example.intervals.json;

import com.fasterxml.jackson.annotation.JsonInclude;
import com.fasterxml.jackson.annotation.JsonProperty;

import java.util.List;
import java.util.Map;

/**
 * Top-level service request.
 *
 * <pre>
 * {
 *   "domain": "time",                 // or "version"
 *   "dataset": "fixture-1",           // optional: load named fixed inputs
 *   "sets": { "A": [ ... ], "B": [ ] }, // optional: explicit input sets
 *   "expression": { "op": "union", "left": {"set":"A"}, "right": {"set":"B"} }
 * }
 * </pre>
 */
@JsonInclude(JsonInclude.Include.NON_NULL)
public record RequestDto(
        @JsonProperty("domain") String domain,
        @JsonProperty("dataset") String dataset,
        @JsonProperty("sets") Map<String, List<IntervalDto>> sets,
        @JsonProperty("expression") ExprDto expression) {
}
