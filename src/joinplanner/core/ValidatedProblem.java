package joinplanner.core;

import java.util.List;

import joinplanner.model.ProblemSpec;
import joinplanner.model.ResolvedEdge;

/**
 * A problem after validation and selectivity resolution: everything the DP needs
 * in index-based form, plus human-readable warnings and derivations.
 *
 * @param spec              original spec (in declared table order)
 * @param resolvedEdges     edges with resolved selectivity, indexed
 * @param adjacency         per table, list of (otherTable, edgeIndex)
 * @param warnings          non-fatal notes (assumed statistics, cross products, ...)
 */
public record ValidatedProblem(
        ProblemSpec spec,
        List<ResolvedEdge> resolvedEdges,
        List<List<int[]>> adjacency,
        List<String> warnings) {

    public int n() {
        return spec.n();
    }
}
