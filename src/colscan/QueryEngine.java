package colscan;

import java.io.IOException;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * Query engine: range filter + aggregate + optional rows, over on-disk shards.
 *
 * Partition pruning contract (implemented in {@link #canPrune}):
 *   1. Statistics may only EXCLUDE a shard proven to have zero matches.
 *   2. Missing min/max/nullCount for a referenced column => the shard is
 *      ALWAYS scanned (false negatives are forbidden).
 *   3. NULL cells are not numbers: an all-NULL shard matches no numeric
 *      comparison, but that conclusion is drawn only from statistics
 *      (nullCount == rows, or absent min/max handled conservatively).
 * Correctness is enforced by comparing every pruned query against a full scan
 * (same physical scan path, pruning disabled) in tests and in /verify.
 */
public final class QueryEngine {

    public enum Mode { PRUNED, FULL_SCAN }

    private final Catalog catalog;

    public QueryEngine(Catalog catalog) {
        this.catalog = catalog;
    }

    // ---------- result shape ----------

    public static final class Result {
        public String table;
        public Mode mode;
        public int shardsTotal;
        public int shardsScanned;
        public List<String> scannedShardIds = new ArrayList<>();
        public List<Map<String, Object>> prunedShards = new ArrayList<>();
        public long matchedRowsCount;
        public Map<String, Object> aggregates = new LinkedHashMap<>();
        public List<Map<String, Object>> rows = new ArrayList<>();
        public long bytesRead;
        public long metadataBytes;
        public long dataBytes;
        public long scannedFileBytes;
        public long totalFileBytes;
        public long prunedFileBytes;
        public List<String> warnings = new ArrayList<>();
    }

    // ---------- pruning ----------

    /** Decision plus a human-readable reason. */
    static final class PruneDecision {
        final boolean prune;
        final String reason;
        PruneDecision(boolean prune, String reason) {
            this.prune = prune;
            this.reason = reason;
        }
        static final PruneDecision SCAN = new PruneDecision(false, null);
    }

    /**
     * Decide whether a shard can be skipped for the given predicate.
     * @param stats stats of the shard keyed by column
     */
    static PruneDecision decide(Predicate pred, int rowCount, Map<String, Shard.Stats> stats) {
        if (pred instanceof Predicate.Comparison) {
            Predicate.Comparison c = (Predicate.Comparison) pred;
            Shard.Stats s = stats.get(c.column);
            if (s == null) {
                return PruneDecision.SCAN; // column without stats => scan
            }
            if (!s.hasMinMax()) {
                // No min/max. We may still prove all-NULL via nullCount...
                if (s.nullCount != null && s.nullCount == rowCount) {
                    return new PruneDecision(true,
                            "column " + c.column + " is all NULL (" + s.nullCount + "/" + rowCount
                                    + "); NULL never satisfies " + c.op);
                }
                return PruneDecision.SCAN; // statistics missing => MUST scan
            }
            if (s.nullCount != null && s.nullCount == rowCount) {
                return new PruneDecision(true,
                        "column " + c.column + " is all NULL; comparison cannot match");
            }
            long mn = s.min, mx = s.max;
            boolean noIntersection;
            String why;
            switch (c.op) {
                case "=": case "==": case "EQ":
                    noIntersection = c.value < mn || c.value > mx;
                    why = c.value + " outside [" + mn + "," + mx + "]";
                    break;
                case "<": case "LT":
                    noIntersection = mn >= c.value;
                    why = "min " + mn + " >= " + c.value + ", nothing < " + c.value;
                    break;
                case "<=": case "LE":
                    noIntersection = mn > c.value;
                    why = "min " + mn + " > " + c.value + ", nothing <= " + c.value;
                    break;
                case ">": case "GT":
                    noIntersection = mx <= c.value;
                    why = "max " + mx + " <= " + c.value + ", nothing > " + c.value;
                    break;
                case ">=": case "GE":
                    noIntersection = mx < c.value;
                    why = "max " + mx + " < " + c.value + ", nothing >= " + c.value;
                    break;
                case "!=": case "<>": case "NE":
                    // Every row equals c.value AND no NULL rows => no row differs.
                    boolean allEqual = mn == mx && mn == c.value;
                    boolean noNulls = s.nullCount != null && s.nullCount == 0;
                    noIntersection = allEqual && noNulls;
                    why = "all values equal " + c.value + " with no NULLs";
                    break;
                default:
                    return PruneDecision.SCAN;
            }
            return noIntersection
                    ? new PruneDecision(true, c.column + " " + why)
                    : PruneDecision.SCAN;
        }
        if (pred instanceof Predicate.NullTest) {
            Predicate.NullTest t = (Predicate.NullTest) pred;
            Shard.Stats s = stats.get(t.column);
            if (s == null) return PruneDecision.SCAN;
            if (t.wantNull) {
                // IS NULL: prune only when we KNOW there are zero nulls.
                if (s.nullCount != null) {
                    if (s.nullCount == 0) {
                        return new PruneDecision(true, t.column + " nullCount=0, IS NULL matches nothing");
                    }
                    return PruneDecision.SCAN;
                }
                return PruneDecision.SCAN; // missing stat => scan
            } else {
                // IS NOT NULL: prune only when we KNOW every row is NULL.
                if (s.nullCount != null && s.nullCount == rowCount) {
                    return new PruneDecision(true, t.column + " all NULL, IS NOT NULL matches nothing");
                }
                return PruneDecision.SCAN;
            }
        }
        if (pred instanceof Predicate.BoolPredicate) {
            Predicate.BoolPredicate b = (Predicate.BoolPredicate) pred;
            if (b.op.equals("AND")) {
                // If ANY conjunct proves zero matches, the AND has none.
                for (Predicate child : b.children) {
                    PruneDecision d = decide(child, rowCount, stats);
                    if (d.prune) {
                        return new PruneDecision(true, "AND: " + d.reason);
                    }
                }
                return PruneDecision.SCAN;
            } else { // OR: prune only when EVERY disjunct independently matches nothing
                List<String> reasons = new ArrayList<>();
                for (Predicate child : b.children) {
                    PruneDecision d = decide(child, rowCount, stats);
                    if (!d.prune) return PruneDecision.SCAN;
                    reasons.add(d.reason);
                }
                return new PruneDecision(true, "OR: all branches empty (" + reasons + ")");
            }
        }
        if (pred instanceof Predicate.NotPredicate) {
            return PruneDecision.SCAN; // conservative: never prune through negation
        }
        return PruneDecision.SCAN;
    }

    // ---------- execution ----------

    public Result query(String table, Predicate filter, List<Aggregator> aggs,
                        boolean includeRows, Mode mode) throws IOException {
        List<ColumnFile.Handle> all = catalog.shards(table);
        Result r = new Result();
        r.table = table;
        r.mode = mode;
        r.shardsTotal = all.size();

        // Columns physically needed: filter inputs + non-count(*) aggregate inputs
        // (+ all columns when rows are returned).
        Set<String> needed = new LinkedHashSet<>(filter.referencedColumns());
        for (Aggregator a : aggs) {
            if (a.column != null) needed.add(a.column);
        }

        Map<String, Aggregator.Acc> globalAgg = Aggregator.newAccumulators(aggs);

        for (ColumnFile.Handle h : all) {
            r.totalFileBytes += java.nio.file.Files.size(h.path);

            PruneDecision d = new PruneDecision(false, null);
            if (mode == Mode.PRUNED) {
                d = decide(filter, h.rowCount, h.stats);
            }
            if (d.prune) {
                Map<String, Object> info = new LinkedHashMap<>();
                info.put("shardId", h.shardId);
                info.put("reason", d.reason);
                info.put("rowCount", h.rowCount);
                info.put("stats", statsView(h));
                r.prunedShards.add(info);
                r.prunedFileBytes += java.nio.file.Files.size(h.path);
                continue;
            }

            r.shardsScanned++;
            r.scannedShardIds.add(h.shardId);
            r.scannedFileBytes += java.nio.file.Files.size(h.path);

            try (ColumnFile.Scanner scanner = ColumnFile.newScanner(h)) {
                scanner.loadMetadata();
                r.metadataBytes += scanner.metadataBytes() + 6; // head + footer per shard

                Set<String> colsThisShard = new LinkedHashSet<>(needed);
                if (includeRows) colsThisShard.addAll(h.columns);
                List<String> openCols = new ArrayList<>();
                for (String c : colsThisShard) {
                    if (h.columns.contains(c)) openCols.add(c);
                }
                Map<String, ColumnFile.ColumnScanner> open = new LinkedHashMap<>();
                for (String c : openCols) open.put(c, scanner.scanColumn(c));

                Map<String, Aggregator.Acc> shardAgg = Aggregator.newAccumulators(aggs);

                for (int row = 0; row < h.rowCount; row++) {
                    final int ri = row;
                    // Per-row cache so repeated references to one column cost one read.
                    Map<String, Value> cache = new LinkedHashMap<>();
                    Predicate.RowAccessor accessor = col -> {
                        Value v = cache.get(col);
                        if (v == null) {
                            ColumnFile.ColumnScanner cs = open.get(col);
                            if (cs == null) v = Value.NULL; // column absent in this shard
                            else {
                                try {
                                    v = cs.isNull(ri) ? Value.NULL : Value.of(cs.getLong(ri));
                                } catch (IOException e) {
                                    throw new RuntimeException(e);
                                }
                            }
                            cache.put(col, v);
                        }
                        return v;
                    };

                    if (!filter.test(accessor)) continue;
                    r.matchedRowsCount++;

                    for (Aggregator a : aggs) {
                        Value v;
                        if (a.kind == Aggregator.Kind.COUNT_ROWS) {
                            v = Value.of(1); // count(*) counts the row itself
                        } else {
                            ColumnFile.ColumnScanner cs = open.get(a.column);
                            v = (cs == null || cs.isNull(ri))
                                    ? Value.NULL : Value.of(cs.getLong(ri));
                        }
                        shardAgg.get(a.alias).addRow(v);
                    }

                    if (includeRows) {
                        Map<String, Object> out = new LinkedHashMap<>();
                        out.put("_shard", h.shardId);
                        for (String c : h.columns) {
                            ColumnFile.ColumnScanner cs = open.get(c);
                            if (cs == null || cs.isNull(ri)) out.put(c, null);
                            else out.put(c, cs.getLong(ri));
                        }
                        r.rows.add(out);
                    }
                }
                for (Map.Entry<String, Aggregator.Acc> e : shardAgg.entrySet()) {
                    globalAgg.get(e.getKey()).merge(e.getValue());
                }
                r.dataBytes += scanner.bytesRead() - scanner.metadataBytes() - 6;
            } catch (RuntimeException re) {
                if (re.getCause() instanceof IOException) throw (IOException) re.getCause();
                throw re;
            }
        }

        for (Aggregator a : aggs) {
            r.aggregates.put(a.alias, globalAgg.get(a.alias).value(a.kind));
        }
        r.bytesRead = r.metadataBytes + r.dataBytes;
        return r;
    }

    private Map<String, Object> statsView(ColumnFile.Handle h) {
        Map<String, Object> view = new LinkedHashMap<>();
        for (Map.Entry<String, Shard.Stats> e : h.stats.entrySet()) {
            Shard.Stats s = e.getValue();
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("min", s.min);
            m.put("max", s.max);
            m.put("nullCount", s.nullCount);
            m.put("present", s.min != null || s.max != null || s.nullCount != null);
            view.put(e.getKey(), m);
        }
        return view;
    }
}
