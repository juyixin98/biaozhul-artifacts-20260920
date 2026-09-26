package com.example.intervals.json;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import com.fasterxml.jackson.annotation.JsonInclude;
import com.fasterxml.jackson.annotation.JsonProperty;

/**
 * A node in the set-algebra expression tree.
 *
 * <p>Exactly one of the following shapes is valid:
 * <ul>
 *   <li>leaf: {@code {"set": "A"}} — references a named input set or a set
 *       from the selected fixed dataset;</li>
 *   <li>unary: {@code {"op": "complement", "arg": node}};</li>
 *   <li>binary: {@code {"op": "union", "left": node, "right": node}}.</li>
 * </ul>
 * Supported binary operations: {@code union} (alias {@code or}),
 * {@code intersection} (alias {@code and}), {@code difference} (aliases
 * {@code minus}, {@code except}); unary: {@code complement} (alias {@code not}).
 */
@JsonInclude(JsonInclude.Include.NON_NULL)
@JsonIgnoreProperties(ignoreUnknown = false)
public record ExprDto(
        @JsonProperty("op") String op,
        @JsonProperty("set") String set,
        @JsonProperty("arg") ExprDto arg,
        @JsonProperty("left") ExprDto left,
        @JsonProperty("right") ExprDto right) {
}
