package com.eventorder.model;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import java.util.List;

/** Top-level request document. */
@JsonIgnoreProperties(ignoreUnknown = true)
public record OrderRequest(
        List<EventInput> events,
        List<DependencyInput> dependencies) {

    public OrderRequest {
        events = events == null ? List.of() : List.copyOf(events);
        dependencies = dependencies == null ? List.of() : List.copyOf(dependencies);
    }
}
