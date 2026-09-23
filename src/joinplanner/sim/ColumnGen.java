package joinplanner.sim;

/**
 * Description of one join column for synthetic data generation.
 *
 * @param table  table name
 * @param column column name (must match an edge column)
 * @param ndv    number of distinct values actually generated
 * @param zipf   Zipf exponent of the value distribution (0 = uniform)
 */
public record ColumnGen(String table, String column, long ndv, double zipf) {
}
