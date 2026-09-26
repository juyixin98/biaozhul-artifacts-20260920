package com.migration.planner.model;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;

import java.util.Map;

/**
 * A path planning request. {@code context} carries the boolean flags that edge
 * preconditions are evaluated against (e.g. maintenance_window, backup_verified).
 */
@JsonIgnoreProperties(ignoreUnknown = true)
public record PlanRequest(String from, String to, Map<String, Boolean> context, Integer maxAlternatives) {

    public Map<String, Boolean> contextOrEmpty() {
        return context == null ? Map.of() : context;
    }
}
