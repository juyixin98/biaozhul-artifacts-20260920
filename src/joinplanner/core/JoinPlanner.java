package joinplanner.core;

import static joinplanner.core.Stats.fromLog;
import static joinplanner.core.Stats.logAdd;
import static joinplanner.core.Stats.logRows;

import java.util.ArrayList;
import java.util.List;

import joinplanner.model.JoinType;
import joinplanner.model.PlanNode;

/**
 * Exhaustive dynamic-programming join-order search (System R / Selinger-style
 * enumeration over table subsets, generalized to <b>bushy</b> plans).
 *
 * <p>For every subset S of tables we keep the cheapest binary tree producing S.
 * Combining A and B is legal when an edge crosses A–B (inner join) or, with
 * {@code allowCrossProducts}, A and B are any two internally-connected
 * components (Cartesian product).
 *
 * <p>Complexity O(3^n) time, O(2^n) space; n ≤ 8.
 *
 * <h2>Cost model</h2>
 * <ul>
 *   <li>output-cardinality(S) = product of base cardinalities · product of
 *       selectivities of all edges fully contained in S (order-independent,
 *       computed per subset in log space);</li>
 *   <li>cost = sum of the estimated output cardinalities of every join node
 *       (base-table scans cost 0).</li>
 * </ul>
 * Ties are broken deterministically (lower left-subset mask wins), so the same
 * request always yields the same plan.
 */
public final class JoinPlanner {

    private final ValidatedProblem p;
    private final int n;
    private final double[] baseLog;
    private final int size;
    private final double[] subsetLogCard;
    private final double[] bestLogCost;
    private final PlanNode[] node;
    private boolean[] connectedCache;
    private boolean costOverflow;

    public JoinPlanner(ValidatedProblem p) {
        this.p = p;
        this.n = p.n();
        this.size = 1 << n;
        this.baseLog = new double[n];
        for (int i = 0; i < n; i++) {
            baseLog[i] = logRows(p.spec().tables().get(i).rows());
        }
        this.subsetLogCard = new double[size];
        this.bestLogCost = new double[size];
        this.node = new PlanNode[size];
    }

    public DpResult plan() {
        computeSubsetCardinalities();
        costOverflow = false;

        for (int mask = 1; mask < size; mask++) {
            if (Integer.bitCount(mask) == 1) {
                int i = Integer.numberOfTrailingZeros(mask);
                node[mask] = PlanNode.leaf(i, fromLog(subsetLogCard[mask]));
                bestLogCost[mask] = Double.NEGATIVE_INFINITY;
                continue;
            }

            boolean found = false;
            double best = Double.POSITIVE_INFINITY;
            int bestL = -1;
            int bestR = -1;
            int bestE = -1;

            int sub = (mask - 1) & mask;
            while (sub != 0) {
                int other = mask ^ sub;
                // Evaluate each bipartition once: canonical side `sub` is the one
                // containing the lowest set bit, and it must be a strict subset.
                int lowBit = mask & -mask;
                if ((sub & lowBit) != 0 && other != 0) {
                    // Every sub-plan must itself be realizable (connected internally,
                    // or cross products must be allowed to build it).
                    boolean subOk = node[sub] != null;
                    boolean otherOk = node[other] != null;
                    if (subOk && otherOk) {
                        int edge = crossingEdge(sub, other);
                        boolean legal = edge >= 0;
                        boolean cross = false;
                        if (!legal && p.spec().allowCrossProducts()
                                && connectedMask(sub) && connectedMask(other)) {
                            legal = true;
                            cross = true;
                        }
                        if (legal) {
                            double logCost = joinLogCost(sub, other);
                            if (!found || logCost < best
                                    || (logCost == best && sub < bestL)) {
                                best = logCost;
                                bestL = sub;
                                bestR = other;
                                bestE = cross ? -1 : edge;
                                found = true;
                            }
                        }
                    }
                }
                sub = (sub - 1) & mask;
            }

            if (!found) {
                // No legal plan builds this subset (it is disconnected internally).
                // Mark it unrealizable; only the full set raises to the caller.
                node[mask] = null;
                bestLogCost[mask] = Double.POSITIVE_INFINITY;
                if (mask == size - 1) {
                    throw new DisconnectedGraphException(
                            GraphComponents.componentsByName(p));
                }
                continue;
            }
            bestLogCost[mask] = best;

            double outRows = fromLog(subsetLogCard[mask]);
            if (Stats.overflowed(outRows) || best == Double.POSITIVE_INFINITY) {
                costOverflow = true;
            }
            node[mask] = PlanNode.join(node[bestL], node[bestR],
                    bestE >= 0 ? JoinType.INNER : JoinType.CROSS,
                    bestE, outRows, fromLog(best));
        }

        int all = size - 1;
        double totalCost = node[all].nodeCost();
        if (Stats.overflowed(totalCost)) {
            costOverflow = true;
        }
        List<Integer> order = new ArrayList<>();
        collectLeaves(node[all], order);
        List<String> names = order.stream()
                .map(i -> p.spec().tables().get(i).name())
                .toList();
        return new DpResult(node[all], totalCost, costOverflow,
                List.copyOf(order), names);
    }

    /** log(cost(A join B)) = log(cost(A) + cost(B) + |A join B|). */
    private double joinLogCost(int a, int b) {
        double logC = logAdd(bestLogCost[a], bestLogCost[b]);
        return logAdd(logC, subsetLogCard[a | b]);
    }

    /**
     * Finds one edge crossing the partition (A, B): one endpoint in A and the
     * other in B. The output-cardinality model multiplies ALL contained edges,
     * so which crossing edge labels the node only affects the explanation; we
     * deterministically pick the lowest edge index.
     */
    private int crossingEdge(int a, int b) {
        for (int i = 0; i < p.resolvedEdges().size(); i++) {
            var e = p.resolvedEdges().get(i);
            int ea = 1 << e.leftIndex();
            int eb = 1 << e.rightIndex();
            boolean aHasL = (a & ea) != 0;
            boolean aHasR = (a & eb) != 0;
            boolean bHasL = (b & ea) != 0;
            boolean bHasR = (b & eb) != 0;
            if ((aHasL && bHasR) || (aHasR && bHasL)) {
                return i;
            }
        }
        return -1;
    }

    private boolean connectedMask(int mask) {
        if (connectedCache == null) {
            connectedCache = new boolean[size];
            for (int m = 1; m < size; m++) {
                connectedCache[m] = computeConnected(m);
            }
        }
        return connectedCache[mask];
    }

    private boolean computeConnected(int mask) {
        int start = Integer.numberOfTrailingZeros(mask);
        int reached = 1 << start;
        int frontier = reached;
        while (frontier != 0) {
            int i = Integer.numberOfTrailingZeros(frontier);
            frontier &= frontier - 1;
            for (int[] nb : p.adjacency().get(i)) {
                int bit = 1 << nb[0];
                if ((mask & bit) != 0 && (reached & bit) == 0) {
                    reached |= bit;
                    frontier |= bit;
                }
            }
        }
        return reached == mask;
    }

    /**
     * log |S| = Σ log base rows + Σ log selectivity over edges fully inside S.
     * A zero-row base table forces -infinity (empty result propagates through joins).
     */
    private void computeSubsetCardinalities() {
        for (int mask = 0; mask < size; mask++) {
            double logProd = 0.0;
            boolean zero = false;
            for (int i = 0; i < n; i++) {
                if ((mask & (1 << i)) != 0) {
                    if (baseLog[i] == Double.NEGATIVE_INFINITY) {
                        zero = true;
                    } else {
                        logProd += baseLog[i];
                    }
                }
            }
            if (zero) {
                subsetLogCard[mask] = Double.NEGATIVE_INFINITY;
                continue;
            }
            for (var e : p.resolvedEdges()) {
                int both = (1 << e.leftIndex()) | (1 << e.rightIndex());
                if ((mask & both) == both) {
                    logProd += Math.log(e.selectivity());
                }
            }
            subsetLogCard[mask] = logProd;
        }
    }

    private void collectLeaves(PlanNode n0, List<Integer> out) {
        if (n0.isLeaf()) {
            out.add(n0.tableIndex());
        } else {
            collectLeaves(n0.left(), out);
            collectLeaves(n0.right(), out);
        }
    }

    /** Estimated cardinality (linear) of a table subset. */
    public double subsetCardinality(int mask) {
        return fromLog(subsetLogCard[mask]);
    }

    public double subsetLogCardinality(int mask) {
        return subsetLogCard[mask];
    }
}
