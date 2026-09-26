package com.example.eventorder.model;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import java.util.List;

/**
 * Reconstruction request envelope.
 *
 * @param requestId    caller-supplied identifier, echoed back in the response
 * @param events       events to order (at least one)
 * @param dependencies explicit partial-order edges; null or missing means none
 * @param options      optional knobs; null means defaults
 */
@JsonIgnoreProperties(ignoreUnknown = false)
public record OrderRequest(String requestId,
                           List<EventSpec> events,
                           List<DependencySpec> dependencies,
                           OrderOptions options) {
}
