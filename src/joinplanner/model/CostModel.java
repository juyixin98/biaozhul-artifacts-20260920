package joinplanner.model;

/**
 * Cost metric used by the dynamic program.
 *
 * <ul>
 *   <li>{@code TOTAL_INTERMEDIATE_ROWS} — classic textbook metric: sum of the estimated
 *       output cardinalities of every internal join node (System R style).</li>
 *   <li>{@code SUM_OF_INPUTS} — each join costs (left input rows + right input rows);
 *       scans of base tables are free. With the output-based cardinality model this is
 *       a different, also common, textbook variant.</li>
 * </ul>
 */
public enum CostModel {
    TOTAL_INTERMEDIATE_ROWS,
    SUM_OF_INPUTS;

    public static CostModel parse(String raw) {
        if (raw == null) {
            return TOTAL_INTERMEDIATE_ROWS;
        }
        switch (raw.trim().toUpperCase().replace('-', '_')) {
            case "TOTAL_INTERMEDIATE_ROWS":
            case "TOTAL":
            case "INTERMEDIATE":
                return TOTAL_INTERMEDIATE_ROWS;
            case "SUM_OF_INPUTS":
            case "INPUTS":
                return SUM_OF_INPUTS;
            default:
                throw new joinplanner.json.BadInputException(
                        "costModel must be TOTAL_INTERMEDIATE_ROWS or SUM_OF_INPUTS");
        }
    }
}
