package joinplanner.plan;

import joinplanner.model.Edge;
import joinplanner.model.Spec;

import java.util.ArrayList;
import java.util.List;

/** Connected components of the join graph over the table index space. */
public final class GraphComponents {

    private GraphComponents() {}

    public static List<Integer> components(Spec spec) {
        int n = spec.n();
        int[] adjacency = new int[n];
        for (Edge e : spec.edges) {
            adjacency[e.leftIndex] |= 1 << e.rightIndex;
            adjacency[e.rightIndex] |= 1 << e.leftIndex;
        }
        List<Integer> masks = new ArrayList<>();
        int seen = 0;
        int all = (1 << n) - 1;
        while (seen != all) {
            int missing = all ^ seen;
            int startBit = Integer.lowestOneBit(missing);
            int frontier = startBit;
            int component = 0;
            while (frontier != 0) {
                int bit = Integer.lowestOneBit(frontier);
                frontier ^= bit;
                if ((component & bit) != 0) {
                    continue;
                }
                component |= bit;
                int idx = Integer.numberOfTrailingZeros(bit);
                frontier |= adjacency[idx] & ~component;
            }
            masks.add(component);
            seen |= component;
        }
        return masks;
    }
}
