package com.opp16.engine;

import com.opp16.engine.json.Json;

import java.util.ArrayList;
import java.util.List;

/**
 * Logical query plan parsed from the JSON request.
 *
 * Tree shape (rendered by {@link #describe()}):
 * <pre>
 * Project / Aggregate
 *   Filter(predicate, batchSize)        <- produces the SelectionVector
 *     TableScan
 * </pre>
 * An externally supplied {@code selection} replaces the TableScan+Filter node;
 * it is a test/injection hook for sparse, duplicate and invalid subscripts.
 */
public final class QueryPlan {

    public static final class AggregateSpec {
        public final String alias;
        public final String function;   // COUNT, SUM, AVG, MIN, MAX
        public final String column;     // null for COUNT(*)

        public AggregateSpec(String alias, String function, String column) {
            this.alias = alias;
            this.function = function;
            this.column = column;
        }
    }

    public Predicate predicate;                 // null == no filter
    public int[] explicitSelection;             // null == scan+filter
    public int batchSize = 1024;
    public List<String> projection;             // null == all columns / default agg output
    public List<String> groupBy = new ArrayList<>();
    public List<AggregateSpec> aggregates = new ArrayList<>();
    public boolean explain;
    public String requestId;

    public boolean hasAggregates() { return !aggregates.isEmpty(); }

    public static QueryPlan fromRequest(Json.Obj req) {
        QueryPlan p = new QueryPlan();
        p.requestId = req.has("requestId") ? req.getString("requestId") : null;
        p.explain = req.getBool("explain", false);
        p.batchSize = req.getInt("batchSize", 1024);
        if (p.batchSize <= 0) throw new EngineException("batchSize must be positive, got " + p.batchSize);

        if (req.has("selection")) {
            Json.Arr arr = req.get("selection").asArray();
            p.explicitSelection = new int[arr.size()];
            for (int i = 0; i < arr.size(); i++) p.explicitSelection[i] = (int) arr.get(i).asLong();
        }
        if (req.has("filter")) {
            if (p.explicitSelection != null) {
                throw new EngineException("'filter' and explicit 'selection' cannot both be set "
                        + "(explicit selection is an injection hook that replaces the filter)");
            }
            p.predicate = Predicate.fromJson(req.get("filter"));
        }
        if (req.has("project")) {
            p.projection = new ArrayList<>();
            for (Json.Value v : req.get("project").asArray().list) p.projection.add(v.asString());
        }
        if (req.has("groupBy")) {
            for (Json.Value v : req.get("groupBy").asArray().list) p.groupBy.add(v.asString());
        }
        if (req.has("aggregate")) {
            for (Json.Value av : req.get("aggregate").asArray().list) {
                Json.Obj a = av.asObject();
                String fn = a.getString("fn").toUpperCase();
                String col = a.has("column") && a.get("column").isString() ? a.getString("column") : null;
                if ("*".equals(col)) col = null; // COUNT(*)
                String alias = a.has("alias") && a.get("alias").isString()
                        ? a.getString("alias") : defaultAlias(fn, col);
                if (col == null && !"COUNT".equals(fn)) {
                    throw new EngineException(fn + " requires a column; only COUNT supports *");
                }
                p.aggregates.add(new QueryPlan.AggregateSpec(alias, fn, col));
            }
        }
        return p;
    }

    private static String defaultAlias(String fn, String col) {
        return col == null ? "count_star" : fn.toLowerCase() + "_" + col;
    }

    /** Human-readable logical plan (the "explain" output / plan export). */
    public String describe() {
        StringBuilder sb = new StringBuilder();
        sb.append(hasAggregates() ? "Aggregate" : "Project");
        if (hasAggregates()) {
            List<String> parts = new ArrayList<>();
            parts.addAll(groupBy);
            for (AggregateSpec a : aggregates) {
                parts.add(a.function.toUpperCase() + "(" + (a.column == null ? "*" : a.column) + ") AS " + a.alias);
            }
            sb.append(" [").append(String.join(", ", parts)).append("]");
        } else if (projection != null) {
            sb.append(" [").append(String.join(", ", projection)).append("]");
        } else {
            sb.append(" [*]");
        }
        sb.append('\n');
        if (explicitSelection != null) {
            sb.append("  ExplicitSelection[").append(explicitSelection.length).append(" indices]\n");
        } else if (predicate != null) {
            sb.append("  Filter(batchSize=").append(batchSize).append(")\n");
            sb.append("    ").append(renderPredicate(predicate, 2)).append('\n');
            sb.append("    TableScan\n");
        } else {
            sb.append("  TableScan\n");
        }
        return sb.toString();
    }

    private static String renderPredicate(Predicate pred, int depth) {
        String pad = "  ".repeat(depth);
        if (pred instanceof Predicate.And a) return "AND\n" + pad + "  " + renderPredicate(a.left, depth + 1)
                + "\n" + pad + "  " + renderPredicate(a.right, depth + 1);
        if (pred instanceof Predicate.Or o) return "OR\n" + pad + "  " + renderPredicate(o.left, depth + 1)
                + "\n" + pad + "  " + renderPredicate(o.right, depth + 1);
        if (pred instanceof Predicate.Not n) return "NOT\n" + pad + "  " + renderPredicate(n.child, depth + 1);
        if (pred instanceof Predicate.Compare c)
            return c.columnName + " " + c.op.text + " " + (c.literal instanceof String ? "'" + c.literal + "'" : c.literal);
        if (pred instanceof Predicate.Between b) return b.columnName + " BETWEEN " + b.low + " AND " + b.high;
        if (pred instanceof Predicate.NullTest nt) return nt.columnName + (nt.wantNull ? " IS NULL" : " IS NOT NULL");
        return pred.toString();
    }
}
