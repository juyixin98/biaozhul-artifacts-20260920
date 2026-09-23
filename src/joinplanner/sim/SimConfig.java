package joinplanner.sim;

import java.util.List;

/**
 * A simulation request: the planner problem plus real data-generation parameters.
 *
 * @param problemJson raw planning request (same shape as /api/plan)
 * @param columns     per-column distributions; tables/columns not listed default to
 *                    uniform over ndv = table cardinality
 * @param seed        RNG seed (deterministic output)
 * @param maxRows     abort guard: maximum number of rows materialized in any
 *                    intermediate relation
 */
public record SimConfig(Object problemJson, List<ColumnGen> columns,
                        long seed, long maxRows) {

    public static final long DEFAULT_MAX_ROWS = 5_000_000L;
}
