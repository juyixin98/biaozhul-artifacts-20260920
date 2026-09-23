package joinplanner.model;

/**
 * An edge after selectivity resolution.
 *
 * @param spec        original edge specification
 * @param leftIndex   index of left table in the problem's table list
 * @param rightIndex  index of right table
 * @param selectivity resolved selectivity in (0, 1]
 * @param source      how the selectivity was obtained
 * @param explanation human-readable derivation of the selectivity
 */
public record ResolvedEdge(
        EdgeSpec spec,
        int leftIndex,
        int rightIndex,
        double selectivity,
        SelectivitySource source,
        String explanation) {
}
