package joinplanner.model;

/**
 * One equi-join predicate: leftTable.leftColumn = rightTable.rightColumn.
 *
 * <p>All statistics are optional. Resolution order when planning:
 * <ol>
 *   <li>explicit {@code selectivity} (must be 0 &lt; s &le; 1)</li>
 *   <li>{@code 1/max(ndvLeft, ndvRight)} from supplied NDVs (missing NDV is assumed
 *       to equal the cardinality of that base table — i.e. column assumed unique)</li>
 *   <li>{@code 1/max(cardLeft, cardRight)} — both sides assumed unique</li>
 * </ol>
 * A global {@code defaultSelectivity} on the request overrides case 3 when configured.
 */
public record EdgeSpec(
        String leftTable,
        String leftColumn,
        String rightTable,
        String rightColumn,
        Double selectivity,
        Long ndvLeft,
        Long ndvRight) {
}
