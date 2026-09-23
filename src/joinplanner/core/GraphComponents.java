package joinplanner.core;

import java.util.ArrayList;
import java.util.List;

/** Union-find connected components of the join graph. */
public final class GraphComponents {

    private GraphComponents() {
    }

    /** Returns components as lists of table indices, smallest index first inside each. */
    public static List<List<Integer>> components(ValidatedProblem p) {
        int n = p.n();
        int[] parent = new int[n];
        for (int i = 0; i < n; i++) {
            parent[i] = i;
        }
        for (var e : p.resolvedEdges()) {
            union(parent, e.leftIndex(), e.rightIndex());
        }
        List<List<Integer>> comps = new ArrayList<>();
        for (int i = 0; i < n; i++) {
            if (find(parent, i) == i) {
                List<Integer> c = new ArrayList<>();
                for (int j = i; j < n; j++) {
                    if (find(parent, j) == i) {
                        c.add(j);
                    }
                }
                comps.add(c);
            }
        }
        return comps;
    }

    public static List<List<String>> componentsByName(ValidatedProblem p) {
        List<List<String>> out = new ArrayList<>();
        for (List<Integer> c : components(p)) {
            List<String> names = c.stream().map(i -> p.spec().tables().get(i).name()).toList();
            out.add(names);
        }
        return out;
    }

    private static int find(int[] parent, int x) {
        while (parent[x] != x) {
            parent[x] = parent[parent[x]];
            x = parent[x];
        }
        return x;
    }

    private static void union(int[] parent, int a, int b) {
        int ra = find(parent, a);
        int rb = find(parent, b);
        if (ra != rb) {
            parent[Math.max(ra, rb)] = Math.min(ra, rb);
        }
    }
}
