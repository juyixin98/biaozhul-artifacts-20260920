package joinplanner.sim;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * Executes real in-memory hash joins over generated data and reports actual
 * intermediate cardinalities for every connected subset.
 *
 * <p>The simulator owns a global set of column <em>slots</em>, one per (table, incident
 * edge) pair. A row is a {@code long[slotCount]} sparse array ({@link Long#MIN_VALUE}
 * marks an absent slot). For every connected subset the tables are added along a
 * spanning-tree order: the newly added table is the hash-build side on one crossing
 * edge, and any further crossing edges (cycles) are applied as extra equality filters
 * on the combined row.
 */
public final class ActualExecutor {

    /** Actual rows for one subset, with a cap flag for aborted materializations. */
    public static final class Actual {
        public final long rows;
        public final boolean capped;

        Actual(long rows, boolean capped) {
            this.rows = rows;
            this.capped = capped;
        }
    }

    private final int n;
    private final int slotCount;
    private final long[][] baseColumn;   // slot -> values of the owning base table
    private final int[] slotOwner;       // slot -> table
    private final SimEdge[] edges;
    private final int[][] edgeSlots;     // edge -> {left slot, right slot}
    private final long cap;
    private final Actual[] memo;

    public ActualExecutor(int n, int slotCount, long[][] baseColumn, int[] slotOwner,
                          SimEdge[] edges, int[][] edgeSlots, long cap) {
        this.n = n;
        this.slotCount = slotCount;
        this.baseColumn = baseColumn;
        this.slotOwner = slotOwner;
        this.edges = edges;
        this.edgeSlots = edgeSlots;
        this.cap = cap;
        this.memo = new Actual[1 << n];
    }

    public Actual actual(int mask) {
        if (memo[mask] != null) {
            return memo[mask];
        }
        if (Integer.bitCount(mask) == 1) {
            int t = Integer.numberOfTrailingZeros(mask);
            Actual a = new Actual(baseColumn[firstSlotOf(t)].length, false);
            memo[mask] = a;
            return a;
        }

        int[] chain = joinChain(mask); // tables in join order
        int seed = chain[0];
        int seedSlot = firstSlotOf(seed);

        List<long[]> rel = new ArrayList<>();
        long[] seedCol = baseColumn[seedSlot];
        for (int i = 0; i < seedCol.length; i++) {
            long[] row = new long[slotCount];
            Arrays.fill(row, Long.MIN_VALUE);
            copyTableRow(row, seed, i);
            rel.add(row);
        }
        int reached = 1 << seed;
        boolean capped = false;

        for (int ci = 1; ci < chain.length; ci++) {
            int added = chain[ci];
            int[] crossing = crossingEdges(added, reached);
            int driving = crossing[0];
            int addedSlot = edgeSlots[driving][0] == slotOf(driving, added)
                    ? edgeSlots[driving][0] : edgeSlots[driving][1];
            int reachedSlot = addedSlot == edgeSlots[driving][0]
                    ? edgeSlots[driving][1] : edgeSlots[driving][0];

            Map<Long, List<Integer>> build = hashBaseColumn(addedSlot);
            List<long[]> next = new ArrayList<>();
            for (long[] reachedRow : rel) {
                List<Integer> matches = build.get(reachedRow[reachedSlot]);
                if (matches == null) {
                    continue;
                }
                for (int baseIdx : matches) {
                    long[] combined = reachedRow.clone();
                    copyTableRow(combined, added, baseIdx);
                    if (passesOtherEdges(combined, crossing, driving)) {
                        next.add(combined);
                        if (next.size() > cap) {
                            capped = true;
                            break;
                        }
                    }
                }
                if (capped) {
                    break;
                }
            }
            rel = next;
            reached |= 1 << added;
            if (capped) {
                break;
            }
        }

        Actual a = capped ? new Actual(cap, true) : new Actual(rel.size(), false);
        memo[mask] = a;
        return a;
    }

    private int firstSlotOf(int table) {
        for (int s = 0; s < slotCount; s++) {
            if (slotOwner[s] == table) {
                return s;
            }
        }
        throw new IllegalStateException("Table " + table + " has no columns");
    }

    private void copyTableRow(long[] row, int table, int baseIdx) {
        for (int s = 0; s < slotCount; s++) {
            if (slotOwner[s] == table) {
                row[s] = baseColumn[s][baseIdx];
            }
        }
    }

    private Map<Long, List<Integer>> hashBaseColumn(int slot) {
        long[] col = baseColumn[slot];
        Map<Long, List<Integer>> h = new HashMap<>(Math.min(col.length * 2, 1 << 20));
        for (int i = 0; i < col.length; i++) {
            h.computeIfAbsent(col[i], k -> new ArrayList<>()).add(i);
        }
        return h;
    }

    private boolean passesOtherEdges(long[] row, int[] crossing, int driving) {
        for (int ei : crossing) {
            if (ei == driving) {
                continue;
            }
            if (row[edgeSlots[ei][0]] != row[edgeSlots[ei][1]]) {
                return false;
            }
        }
        return true;
    }

    private int slotOf(int edgeIndex, int table) {
        return edges[edgeIndex].leftIndex == table
                ? edgeSlots[edgeIndex][0] : edgeSlots[edgeIndex][1];
    }

    private int[] crossingEdges(int added, int reachedMask) {
        List<Integer> list = new ArrayList<>();
        for (int ei = 0; ei < edges.length; ei++) {
            SimEdge e = edges[ei];
            int other = -1;
            if (e.leftIndex == added) {
                other = e.rightIndex;
            } else if (e.rightIndex == added) {
                other = e.leftIndex;
            }
            if (other >= 0 && (reachedMask & (1 << other)) != 0) {
                list.add(ei);
            }
        }
        int[] arr = new int[list.size()];
        for (int i = 0; i < arr.length; i++) {
            arr[i] = list.get(i);
        }
        return arr;
    }

    /** Spanning-tree growth order for a connected mask. */
    private int[] joinChain(int mask) {
        List<Integer> chain = new ArrayList<>();
        int start = Integer.numberOfTrailingZeros(mask);
        chain.add(start);
        int reached = 1 << start;
        while (reached != mask) {
            int frontier = mask & ~reached;
            int chosen = -1;
            int x = frontier;
            outer:
            while (x != 0) {
                int bit = Integer.lowestOneBit(x);
                int t = Integer.numberOfTrailingZeros(bit);
                for (int ei = 0; ei < edges.length; ei++) {
                    SimEdge e = edges[ei];
                    int other = -1;
                    if (e.leftIndex == t) {
                        other = e.rightIndex;
                    } else if (e.rightIndex == t) {
                        other = e.leftIndex;
                    }
                    if (other >= 0 && (reached & (1 << other)) != 0) {
                        chosen = t;
                        break outer;
                    }
                }
                x ^= bit;
            }
            if (chosen < 0) {
                throw new IllegalStateException("Mask " + Integer.toBinaryString(mask) + " not connected");
            }
            chain.add(chosen);
            reached |= 1 << chosen;
        }
        int[] arr = new int[chain.size()];
        for (int i = 0; i < arr.length; i++) {
            arr[i] = chain.get(i);
        }
        return arr;
    }
}
