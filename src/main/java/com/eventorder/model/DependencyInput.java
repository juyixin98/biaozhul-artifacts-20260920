package com.eventorder.model;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;

/** Explicit user-declared ordering constraint: {@code before} must precede {@code after}. */
@JsonIgnoreProperties(ignoreUnknown = true)
public record DependencyInput(String before, String after, String reason) {
}
