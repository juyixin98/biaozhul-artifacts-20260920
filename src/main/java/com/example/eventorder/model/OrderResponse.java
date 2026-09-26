package com.example.eventorder.model;

import java.util.List;

/**
 * Reconstruction response envelope.
 *
 * @param requestId        echoed from the request
 * @param tzdbVersion      IANA time-zone database version of the JDK running the engine
 *                         (recorded, never used to alter results)
 * @param satisfiable      true when at least one valid order exists
 * @param order            the deterministic topological order (lexicographically smallest
 *                         valid order); null when unsatisfiable
 * @param validOrderCount  total number of distinct valid orders (exact, may be 0)
 * @param enumeratedOrders up to {@code maxEnumeratedOrders} concrete valid orders,
 *                         lexicographically sorted; empty when unsatisfiable
 * @param dependencyChecks per-dependency verification, one entry per requested edge
 * @param conflicts        minimal readable conflict chains; empty when satisfiable
 */
public record OrderResponse(String requestId,
                            String tzdbVersion,
                            boolean satisfiable,
                            List<String> order,
                            long validOrderCount,
                            List<List<String>> enumeratedOrders,
                            List<DependencyCheck> dependencyChecks,
                            List<Conflict> conflicts) {
}
