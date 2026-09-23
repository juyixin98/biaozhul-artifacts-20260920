package joinplanner.sim;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import joinplanner.core.ValidatedProblem;

/**
 * Executes equi-joins over generated data for real and records exact
 * intermediate cardinalities.
 *
 * <p>For every table subset S (built in increasing size via a spanning-tree
 * order) it materializes the multiset of tuples
 * {@code (values of every edge-referenced column of every table in S)} produced
 * by joining the base tables on ALL edges inside S. A tuple carries its
 * multiplicity, so memory is bounded by distinct combined keys, not raw rows.
 *
 * <p>The resulting multiset is order-independent (relational algebra), which is
 * exactly what we need to compare the true cost of different join orders.
 */
public final class JoinExecutor {

    /** One column position: table index + column name. */
    public record Slot(int tableIndex, String column) {
    }

    /** Materialized intermediate relation for one subset. */
    public static final class Relation {
        final List<Slot> schema;
        final Map<Key, long[]> groups;
        final long totalRows;

        Relation(List<Slot> schema, Map<Key, long[]> groups, long totalRows) {
            this.schema = schema;
            this.groups = groups;
            this.totalRows = totalRows;
        }

        public long totalRows() {
            return totalRows;
        }

        int position(int tableIndex, String column) {
            for (int i = 0; i < schema.size(); i++) {
                Slot s = schema.get(i);
                if (s.tableIndex() == tableIndex && s.column().equals(column)) {
                    return i;
                }
            }
            throw new IllegalStateException(
                    "column " + tableIndex + "." + column + " not in relation");
        }
    }

    private final ValidatedProblem problem;
    private final Map<String, TableData> tables;
    private final long maxRows;
    private final Relation[] rels;
    private final int n;

    public JoinExecutor(ValidatedProblem problem, Map<String, TableData> tables, long maxRows) {
        this.problem = problem;
        this.tables = tables;
        this.maxRows = maxRows;
        this.n = problem.n();
        this.rels = new Relation[1 << n];
    }

    /** Exact cardinality of joining all tables in {@code mask} on all internal edges. */
    public long exactCardinality(int mask) {
        if (rels[mask] == null) {
            build(mask);
        }
        return rels[mask].totalRows;
    }

    /**
     * Builds the relation for {@code mask} by repeatedly joining on a spanning
     * tree. The aggregate result does not depend on build order.
     */
    private void build(int mask) {
        List<Integer> order = spanningOrder(mask);

        int first = order.get(0);
        int builtMask = 1 << first;
        rels[builtMask] = baseRelation(first);

        for (int k = 1; k < order.size(); k++) {
            int ti = order.get(k);
            Relation cur = rels[builtMask];
            Relation base = baseRelation(ti);
            int edgeIdx = edgeBetween(ti, builtMask);
            var edge = problem.resolvedEdges().get(edgeIdx).spec();
            int li = problem.resolvedEdges().get(edgeIdx).leftIndex();
            int ri = problem.resolvedEdges().get(edgeIdx).rightIndex();

            int curTable = (builtMask & (1 << li)) != 0 ? li : ri;
            int newTable = ti;
            String curCol = curTable == li ? edge.leftColumn() : edge.rightColumn();
            String newCol = newTable == li ? edge.leftColumn() : edge.rightColumn();

            Relation merged = hashJoinAggregate(cur, curTable, curCol,
                    base, newTable, newCol, mask);
            builtMask |= 1 << ti;
            rels[builtMask] = merged;
        }
    }

    private int edgeBetween(int tableIndex, int builtMask) {
        for (int[] nb : problem.adjacency().get(tableIndex)) {
            if ((builtMask & (1 << nb[0])) != 0) {
                return nb[1];
            }
        }
        return -1;
    }

    /** Repeatedly add a table adjacent to the built set (subset must be connected). */
    private List<Integer> spanningOrder(int mask) {
        List<Integer> order = new ArrayList<>();
        int start = Integer.numberOfTrailingZeros(mask);
        int built = 1 << start;
        order.add(start);
        while (built != mask) {
            boolean progressed = false;
            for (int i = 0; i < n; i++) {
                if ((mask & (1 << i)) != 0 && (built & (1 << i)) == 0
                        && edgeBetween(i, built) >= 0) {
                    order.add(i);
                    built |= 1 << i;
                    progressed = true;
                }
            }
            if (!progressed) {
                throw new IllegalStateException("subset is not connected: " + subsetName(mask));
            }
        }
        return order;
    }

    private Relation baseRelation(int tableIndex) {
        if (rels[1 << tableIndex] != null) {
            return rels[1 << tableIndex];
        }
        String name = problem.spec().tables().get(tableIndex).name();
        TableData td = tables.get(name);
        List<Slot> schema = new ArrayList<>();
        for (String c : td.columns()) {
            schema.add(new Slot(tableIndex, c));
        }
        int rows = td.rows();
        Map<Key, long[]> groups = new LinkedHashMap<>();
        for (int r = 0; r < rows; r++) {
            int[] vals = new int[schema.size()];
            for (int c = 0; c < schema.size(); c++) {
                vals[c] = td.column(schema.get(c).column())[r];
            }
            addGroup(groups, new Key(vals), 1L);
        }
        Relation rel = new Relation(schema, groups, rows);
        rels[1 << tableIndex] = rel;
        return rel;
    }

    private void addGroup(Map<Key, long[]> groups, Key k, long mult) {
        long[] cur = groups.get(k);
        if (cur == null) {
            groups.put(k, new long[]{mult});
        } else {
            cur[0] += mult;
        }
    }

    /**
     * Hash join of an accumulated relation {@code a} with one base table {@code b},
     * aggregated to the combined schema. Output multiplicity per matched group
     * pair is multA * multB.
     */
    private Relation hashJoinAggregate(Relation a, int tableA, String colA,
                                       Relation b, int tableB, String colB,
                                       int targetMask) {
        int posA = a.position(tableA, colA);
        int posB = b.position(tableB, colB);

        // probe with a (usually larger), build hash table on the small base side b
        Map<Integer, List<Map.Entry<Key, long[]>>> bIndex = new HashMap<>();
        for (Map.Entry<Key, long[]> e : b.groups.entrySet()) {
            bIndex.computeIfAbsent(e.getKey().vals[posB], k -> new ArrayList<>()).add(e);
        }

        List<Slot> schema = new ArrayList<>(a.schema);
        // Keep ALL columns of the new table, including its join column: a later
        // edge in the chain may join on it. (The value equals the left join
        // column for matched pairs.)
        schema.addAll(b.schema);

        Map<Key, long[]> out = new LinkedHashMap<>();
        long total = 0;
        for (Map.Entry<Key, long[]> e : a.groups.entrySet()) {
            List<Map.Entry<Key, long[]>> matches = bIndex.get(e.getKey().vals[posA]);
            if (matches == null) {
                continue;
            }
            for (Map.Entry<Key, long[]> m : matches) {
                long mult = e.getValue()[0] * m.getValue()[0];
                Key combined = combineKeys(e.getKey(), m.getKey());
                addGroup(out, combined, mult);
                total += mult;
                if (total > maxRows) {
                    throw new SimOverflowException(maxRows, subsetName(targetMask));
                }
            }
        }
        return new Relation(schema, out, total);
    }

    /** Concatenate the full value arrays of both sides. */
    private static Key combineKeys(Key ka, Key kb) {
        int[] vals = new int[ka.vals.length + kb.vals.length];
        System.arraycopy(ka.vals, 0, vals, 0, ka.vals.length);
        System.arraycopy(kb.vals, 0, vals, ka.vals.length, kb.vals.length);
        return new Key(vals);
    }

    private String subsetName(int mask) {
        List<String> names = new ArrayList<>();
        for (int i = 0; i < n; i++) {
            if ((mask & (1 << i)) != 0) {
                names.add(problem.spec().tables().get(i).name());
            }
        }
        return names.toString();
    }

    /** Immutable int-array key with value equality (used in hash maps). */
    record Key(int[] vals) {
        @Override
        public boolean equals(Object o) {
            return (o instanceof Key k) && java.util.Arrays.equals(vals, k.vals);
        }

        @Override
        public int hashCode() {
            return java.util.Arrays.hashCode(vals);
        }
    }
}
