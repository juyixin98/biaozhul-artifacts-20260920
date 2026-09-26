package com.example.eventorder.model;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;

/**
 * One explicit partial-order dependency: {@code before} must be ordered before {@code after}.
 *
 * @param before id of the event that must come first
 * @param after  id of the event that must come later
 * @param reason optional human-readable provenance of this dependency
 */
@JsonIgnoreProperties(ignoreUnknown = false)
public record DependencySpec(String before, String after, String reason) {
}
