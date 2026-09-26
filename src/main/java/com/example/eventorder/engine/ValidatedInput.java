package com.example.eventorder.engine;

import com.example.eventorder.model.DependencySpec;
import java.util.List;
import java.util.Map;

/**
 * Validated, engine-ready request.
 *
 * @param requestId           echoed request id (may be null)
 * @param events              validated events keyed by id, sorted by id for determinism
 * @param dependencies        validated dependency edges (self-loops kept: they surface as cycles)
 * @param enforceVersionOrder whether the version-order rule is active
 * @param maxEnumeratedOrders cap for concrete order enumeration
 */
public record ValidatedInput(String requestId,
                             Map<String, ValidatedEvent> events,
                             List<DependencySpec> dependencies,
                             boolean enforceVersionOrder,
                             int maxEnumeratedOrders) {
}
