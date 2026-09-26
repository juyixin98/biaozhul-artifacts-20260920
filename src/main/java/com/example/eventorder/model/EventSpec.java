package com.example.eventorder.model;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;

/**
 * One event in the reconstruction request.
 *
 * @param id       unique event identifier (required, non-blank)
 * @param earliest ISO-8601 start of the feasible time window, offset required
 *                 (e.g. "2026-01-01T09:00:00Z" or "2026-01-01T17:00:00+08:00"); nullable
 * @param latest   ISO-8601 end of the feasible time window, offset required; nullable
 * @param version  optional logical version used by the version-order rule
 */
@JsonIgnoreProperties(ignoreUnknown = false)
public record EventSpec(String id, String earliest, String latest, Long version) {
}
