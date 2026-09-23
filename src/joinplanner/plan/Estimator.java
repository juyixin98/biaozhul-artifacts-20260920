package joinplanner.plan;

import joinplanner.model.Edge;
import joinplanner.model.Spec;

/**
 * Precomputes, for every subset mask:
 * <ul>
 *   <li>{@code connected[mask]} — the subgraph induced by the tables in the mask is
 *       connected (via join edges). The DP only builds connected relations.</li>
 *   <li>{@code card[mask]} — estimated cardinality of the join of all tables in the
 *       mask. Order-independent by construction:
 *       card(S ∪ {a}) = card(S) · ∏ sel(e) over edges e between a and S.</li>
 *   <li>{@code saturated[mask]} — multiplication overflowed and was clamped to
 *       {@code spec.estimateCap}.</li>
 * </ul>
 */
public final class Estimator {

    private final Spec spec;
    private final int n;
    private final int fullMask;
    private final Edge[] edges;
    private final int[] adjacency;
    private final double[] card;
    private final boolean[] connected;
    private final boolean[] saturated;

    public Estimator(Spec spec) {
        this.spec = spec;
        this.n = spec.n();
        this.fullMask = (1 << n) - 1;
        this.edges = spec.edges.toArray(new Edge[0]);
        int size = 1 << n;
        this.adjacency = new int[n];
        this.card = new double[size];
        this.connected = new boolean[size];
        this.saturated = new boolean[size];
        for (Edge e : edges) {
            adjacency[e.leftIndex] |= 1 << e.rightIndex;
            adjacency[e.rightIndex] |= 1 << e.leftIndex;
        }
        deriveEdgeSelectivities();
        compute();
    }

    public double card(int mask) {
        return card[mask];
    }

    public boolean connected(int mask) {
        return connected[mask];
    }

    public boolean saturated(int mask) {
        return saturated[mask];
    }

    public int fullMask() {
        return fullMask;
    }

    /** Product of selectivities of edges with one endpoint in a and the other in b. */
    public double crossingSelectivity(int a, int b) {
        double product = 1.0;
        for (Edge e : edges) {
            boolean lInA = (a & (1 << e.leftIndex)) != 0;
            boolean rInA = (a & (1 << e.rightIndex)) != 0;
            boolean lInB = (b & (1 << e.leftIndex)) != 0;
            boolean rInB = (b & (1 << e.rightIndex)) != 0;
            if ((lInA && rInB) || (rInA && lInB)) {
                product = saturatingMultiply(product, e.derivedSelectivity);
            }
        }
        return product;
    }

    /** Whether at least one join edge has endpoints on both sides. */
    public boolean hasCrossingEdge(int a, int b) {
        int x = a;
        while (x != 0) {
            int bit = Integer.lowestOneBit(x);
            int idx = Integer.numberOfTrailingZeros(bit);
            if ((adjacency[idx] & b) != 0) {
                return true;
            }
            x ^= bit;
        }
        return false;
    }

    private void deriveEdgeSelectivities() {
        for (Edge e : edges) {
            double leftRows = spec.tables.get(e.leftIndex).rows();
            double rightRows = spec.tables.get(e.rightIndex).rows();
            if (e.selectivity != null) {
                e.derivedSelectivity = e.selectivity;
                e.selectivitySource = "explicit selectivity in request";
                continue;
            }
            if (e.leftNdv != null && e.rightNdv != null) {
                e.derivedSelectivity = 1.0 / Math.max(e.leftNdv, e.rightNdv);
                e.selectivitySource = "1 / max(leftNdv=" + e.leftNdv + ", rightNdv=" + e.rightNdv
                        + ") under containment + uniformity";
                if (e.leftNdv > leftRows) {
                    spec.warn("Edge " + e.left + "–" + e.right + ": leftNdv " + e.leftNdv
                            + " exceeds left table rows " + (long) leftRows + " (impossible NDV)");
                }
                if (e.rightNdv > rightRows) {
                    spec.warn("Edge " + e.left + "–" + e.right + ": rightNdv " + e.rightNdv
                            + " exceeds right table rows " + (long) rightRows + " (impossible NDV)");
                }
                continue;
            }
            if (e.leftUnique || e.rightUnique) {
                double sel;
                if (e.leftUnique && e.rightUnique) {
                    // |R ⋈ S| ≤ min(rows); divide by product → 1/max(rows).
                    sel = 1.0 / Math.max(leftRows, rightRows);
                    e.selectivitySource = "1 / max(leftRows, rightRows) — both sides unique keys";
                } else if (e.leftUnique) {
                    sel = 1.0 / leftRows;
                    e.selectivitySource = "1 / leftRows — left column is a unique key";
                } else {
                    sel = 1.0 / rightRows;
                    e.selectivitySource = "1 / rightRows — right column is a unique key";
                }
                e.derivedSelectivity = sel;
                continue;
            }
            if (e.leftNdv != null) {
                e.derivedSelectivity = 1.0 / e.leftNdv;
                e.selectivitySource = "1 / leftNdv=" + e.leftNdv + " (only left NDV supplied; uniformity)";
                continue;
            }
            if (e.rightNdv != null) {
                e.derivedSelectivity = 1.0 / e.rightNdv;
                e.selectivitySource = "1 / rightNdv=" + e.rightNdv + " (only right NDV supplied; uniformity)";
                continue;
            }
            // Missing estimate: the dangerous case. Fall back to a documented flat default
            // rather than crashing, and flag it loudly in the response.
            e.derivedSelectivity = spec.defaultSelectivity;
            e.selectivitySource = "default " + spec.defaultSelectivity
                    + " — no selectivity/NDV/uniqueness supplied (pure guess)";
            spec.warn("Edge " + e.left + "–" + e.right + " has no selectivity information; using default "
                    + spec.defaultSelectivity + ". Plan quality depends on this guess.");
        }
    }

    private void compute() {
        for (int mask = 1; mask <= fullMask; mask++) {
            if (Integer.bitCount(mask) == 1) {
                int i = Integer.numberOfTrailingZeros(mask);
                double rows = spec.tables.get(i).rows();
                saturated[mask] = rows > spec.estimateCap;
                card[mask] = saturated[mask] ? spec.estimateCap : rows;
                connected[mask] = true;
                continue;
            }

            // Cardinality is order-independent: multiply the base row counts by the
            // selectivity of every edge fully inside the mask.
            double value = 1.0;
            boolean over = false;
            for (int i = 0; i < n; i++) {
                if ((mask & (1 << i)) != 0) {
                    double rows = spec.tables.get(i).rows();
                    over |= rows >= spec.estimateCap;
                    value *= Math.min(rows, spec.estimateCap);
                }
            }
            for (Edge e : edges) {
                int edgeMask = (1 << e.leftIndex) | (1 << e.rightIndex);
                if ((mask & edgeMask) == edgeMask) {
                    value *= e.derivedSelectivity;
                }
            }
            if (Double.isInfinite(value) || Double.isNaN(value) || value >= spec.estimateCap) {
                over = true;
                value = spec.estimateCap;
            }
            card[mask] = value;
            saturated[mask] = over;

            // Connectivity: grow a reachable set from the lowest table in the mask.
            int startBit = Integer.lowestOneBit(mask);
            int reached = startBit;
            int frontier = startBit;
            while (frontier != 0) {
                int bit = Integer.lowestOneBit(frontier);
                frontier ^= bit;
                int idx = Integer.numberOfTrailingZeros(bit);
                int fresh = adjacency[idx] & mask & ~reached;
                reached |= fresh;
                frontier |= fresh;
            }
            connected[mask] = reached == mask;
        }
    }

    /**
     * Multiplication clamped to {@code estimateCap} instead of overflowing to
     * {@code Infinity} (which would poison comparisons across the DP).
     */
    private double saturatingMultiply(double a, double b) {
        if (a <= 0 || b <= 0) {
            return 0;
        }
        double r = a * b;
        if (Double.isInfinite(r) || Double.isNaN(r) || r > spec.estimateCap) {
            return spec.estimateCap;
        }
        return r;
    }
}
