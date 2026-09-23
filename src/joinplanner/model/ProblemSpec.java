package joinplanner.model;

import java.util.List;

/**
 * A validated planning request.
 *
 * @param tables             1..8 base tables
 * @param edges              equi-join predicates
 * @param defaultSelectivity fallback selectivity when no statistics exist for an edge
 *                           (null = assume both keys unique, 1/max(cardL,cardR))
 * @param allowCrossProducts if true, disconnected graphs are joined with Cartesian products
 *                           (selectivity 1) instead of producing a DISCONNECTED_GRAPH error
 */
public record ProblemSpec(
        List<TableSpec> tables,
        List<EdgeSpec> edges,
        Double defaultSelectivity,
        boolean allowCrossProducts) {

    public int n() {
        return tables.size();
    }
}
