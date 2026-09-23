package hllengine.engine;

import hllengine.api.ApiException;
import hllengine.hll.HllConfig;
import hllengine.hll.HllSketch;
import hllengine.json.Json;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * Logical plan: a linear pipeline parsed from the request JSON.
 *
 * <pre>
 * scan(table) -> filter? -> [project?] -> aggregate? -> limit?
 * </pre>
 *
 * The same structure doubles as the exportable/explanable plan tree: every
 * node serializes to a readable map ({@link #describe()}). A physical plan is
 * produced by {@link #plan()}; it currently mirrors the logical tree but is a
 * separate object so that the "logical vs physical" boundary is real and
 * visible in {@code explain} output.
 */
public final class LogicalPlan {

    /** Base class of plan nodes. */
    public abstract static class Node {
        public abstract String nodeName();
        public abstract Map<String, Object> describe();
    }

    public static final class Scan extends Node {
        final String dataset;

        Scan(String dataset) {
            this.dataset = dataset;
        }

        @Override
        public String nodeName() {
            return "Scan";
        }

        @Override
        public Map<String, Object> describe() {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("node", "Scan");
            m.put("dataset", dataset);
            return m;
        }
    }

    public static final class Filter extends Node {
        final Predicate predicate;
        final String source;

        Filter(Predicate predicate, String source) {
            this.predicate = predicate;
            this.source = source;
        }

        @Override
        public String nodeName() {
            return "Filter";
        }

        @Override
        public Map<String, Object> describe() {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("node", "Filter");
            m.put("predicate", source);
            return m;
        }
    }

    public static final class Project extends Node {
        final List<String> fields;

        Project(List<String> fields) {
            this.fields = fields;
        }

        @Override
        public String nodeName() {
            return "Project";
        }

        @Override
        public Map<String, Object> describe() {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("node", "Project");
            m.put("fields", fields);
            return m;
        }
    }

    public static final class Aggregate extends Node {
        final List<String> groupBy;
        final List<AggSpec> aggregates;

        Aggregate(List<String> groupBy, List<AggSpec> aggregates) {
            this.groupBy = groupBy;
            this.aggregates = aggregates;
        }

        @Override
        public String nodeName() {
            return "Aggregate";
        }

        @Override
        public Map<String, Object> describe() {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("node", "Aggregate");
            m.put("groupBy", groupBy);
            List<Object> aggs = new ArrayList<>();
            for (AggSpec s : aggregates) {
                aggs.add(s.describe());
            }
            m.put("aggregates", aggs);
            return m;
        }
    }

    public static final class Limit extends Node {
        final int limit;

        Limit(int limit) {
            this.limit = limit;
        }

        @Override
        public String nodeName() {
            return "Limit";
        }

        @Override
        public Map<String, Object> describe() {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("node", "Limit");
            m.put("limit", limit);
            return m;
        }
    }

    // --------------------------------------------------------------- parsing

    private final List<Node> nodes;
    private final Map<String, Object> original;

    private LogicalPlan(List<Node> nodes, Map<String, Object> original) {
        this.nodes = nodes;
        this.original = original;
    }

    public static LogicalPlan parse(Map<String, Object> planJson) {
        try {
            return parseUnchecked(planJson);
        } catch (hllengine.json.JsonException je) {
            throw new ApiException(ApiException.BAD_FORMAT, je.getMessage());
        }
    }

    private static LogicalPlan parseUnchecked(Map<String, Object> planJson) {
        String dataset = Json.asString(planJson.get("dataset"), "plan.dataset");
        List<Node> nodes = new ArrayList<>();
        nodes.add(new Scan(dataset));

        if (planJson.containsKey("filter")) {
            Object f = planJson.get("filter");
            // Parse once for execution, retain the source fragment for explain output.
            Predicate predicate = Predicate.fromJson(f);
            nodes.add(new Filter(predicate, Json.write(f)));
        }

        if (planJson.containsKey("project")) {
            List<Object> fields = Json.asArray(planJson.get("project"), "plan.project");
            List<String> names = new ArrayList<>();
            for (Object f : fields) {
                names.add(Json.asString(f, "plan.project[]"));
            }
            nodes.add(new Project(names));
        }

        if (planJson.containsKey("aggregate")) {
            Map<String, Object> agg = Json.asObject(planJson.get("aggregate"), "plan.aggregate");
            List<String> groupBy = new ArrayList<>();
            if (agg.containsKey("groupBy")) {
                for (Object g : Json.asArray(agg.get("groupBy"), "plan.aggregate.groupBy")) {
                    groupBy.add(Json.asString(g, "plan.aggregate.groupBy[]"));
                }
            }
            List<Object> rawAggs = Json.asArray(agg.get("aggregates"), "plan.aggregate.aggregates");
            if (rawAggs.isEmpty()) {
                throw new ApiException(ApiException.BAD_REQUEST,
                        "plan.aggregate.aggregates must contain at least one aggregate");
            }
            List<AggSpec> specs = new ArrayList<>();
            for (int k = 0; k < rawAggs.size(); k++) {
                specs.add(AggSpec.parse(rawAggs.get(k), k));
            }
            AggSpec.validateUnique(specs);
            nodes.add(new Aggregate(groupBy, specs));
        } else {
            for (String key : new String[] {"groupBy", "aggregates"}) {
                if (planJson.containsKey(key)) {
                    throw new ApiException(ApiException.BAD_REQUEST,
                            "top-level \"" + key + "\" must be nested inside \"aggregate\"");
                }
            }
        }

        if (planJson.containsKey("limit")) {
            int limit = Json.asInt(planJson.get("limit"), "plan.limit", 0, Integer.MAX_VALUE);
            nodes.add(new Limit(limit));
        }

        List<String> allowed = List.of("dataset", "filter", "project", "aggregate", "limit");
        for (String key : planJson.keySet()) {
            if (!allowed.contains(key)) {
                throw new ApiException(ApiException.BAD_FORMAT,
                        "unknown plan field \"" + key + "\"; allowed fields are " + allowed);
            }
        }
        return new LogicalPlan(nodes, planJson);
    }

    public String dataset() {
        return ((Scan) nodes.get(0)).dataset;
    }

    public Map<String, Object> original() {
        return original;
    }

    public List<Node> nodes() {
        return nodes;
    }

    /** Exportable logical plan tree. */
    public Map<String, Object> describe() {
        Map<String, Object> out = new LinkedHashMap<>();
        List<Object> tree = new ArrayList<>();
        for (Node n : nodes) {
            tree.add(n.describe());
        }
        out.put("logicalPlan", tree);
        return out;
    }

    /** Produces the physical plan (currently a 1:1 mapping with cost estimates). */
    public PhysicalPlan plan() {
        return PhysicalPlan.from(this);
    }

    // ------------------------------------------------------------- execution

    /**
     * Executes the pipeline against the catalog. Returns a result containing
     * either a list of projected rows or one row per aggregate group.
     */
    public QueryResult execute(java.util.function.Function<String, Dataset> lookup) {
        Dataset ds = lookup.apply(dataset());

        List<Map<String, Object>> rows;
        List<String> outputColumns;

        Filter filter = find(Filter.class);
        if (filter != null) {
            List<Map<String, Object>> kept = new ArrayList<>();
            for (Map<String, Object> row : ds.rows()) {
                if (filter.predicate.test(row)) kept.add(row);
            }
            rows = kept;
        } else {
            rows = new ArrayList<>(ds.rows());
        }

        long rowsScanned = ds.size();
        long rowsAfterFilter = rows.size();

        Aggregate aggregate = find(Aggregate.class);
        Project project = find(Project.class);
        Limit limit = find(Limit.class);

        if (aggregate != null) {
            if (project != null) {
                throw new ApiException(ApiException.BAD_REQUEST,
                        "\"project\" cannot be combined with \"aggregate\" in this engine");
            }
            return runAggregate(rows, aggregate, limit, rowsScanned, rowsAfterFilter);
        }

        if (project != null) {
            validateColumns(project.fields, ds, "plan.project");
            List<Map<String, Object>> projected = new ArrayList<>();
            for (Map<String, Object> row : rows) {
                Map<String, Object> pr = new LinkedHashMap<>();
                for (String f : project.fields) {
                    pr.put(f, row.get(f));
                }
                projected.add(pr);
            }
            rows = projected;
            outputColumns = project.fields;
        } else {
            outputColumns = ds.columns();
        }

        boolean truncated = false;
        if (limit != null && rows.size() > limit.limit) {
            rows = rows.subList(0, limit.limit);
            truncated = true;
        }
        return QueryResult.ofRows(outputColumns, rows, rowsScanned, rowsAfterFilter, truncated);
    }

    private QueryResult runAggregate(List<Map<String, Object>> rows, Aggregate agg,
                                     Limit limit, long rowsScanned, long rowsAfterFilter) {
        validateColumns(agg.groupBy, null, "plan.aggregate.groupBy");

        // Group key: list of group-by values in order.
        Map<List<Object>, List<Map<String, Object>>> groups = new LinkedHashMap<>();
        for (Map<String, Object> row : rows) {
            List<Object> key = new ArrayList<>();
            for (String g : agg.groupBy) {
                key.add(row.get(g));
            }
            groups.computeIfAbsent(key, k -> new ArrayList<>()).add(row);
        }

        List<Map<String, Object>> outRows = new ArrayList<>();
        for (Map.Entry<List<Object>, List<Map<String, Object>>> entry : groups.entrySet()) {
            List<Map<String, Object>> groupRows = entry.getValue();
            Map<String, Object> out = new LinkedHashMap<>();
            for (int k = 0; k < agg.groupBy.size(); k++) {
                out.put(agg.groupBy.get(k), entry.getKey().get(k));
            }
            for (AggSpec spec : agg.aggregates) {
                computeAggregate(spec, groupRows, out);
            }
            outRows.add(out);
        }

        List<String> outColumns = new ArrayList<>(agg.groupBy);
        for (AggSpec s : agg.aggregates) {
            outColumns.add(s.alias());
            if (s.approximate()) outColumns.add(s.errorAlias());
        }
        outColumns.add("_groupSize");

        boolean truncated = false;
        if (limit != null && outRows.size() > limit.limit) {
            outRows = outRows.subList(0, limit.limit);
            truncated = true;
        }
        return QueryResult.ofGroups(outColumns, outRows, rowsScanned, rowsAfterFilter, truncated,
                agg.groupBy, agg.aggregates);
    }

    private void computeAggregate(AggSpec spec, List<Map<String, Object>> groupRows,
                                  Map<String, Object> out) {
        String field = spec.field();
        switch (spec.kind()) {
            case COUNT:
                out.put(spec.alias(), (long) groupRows.size());
                return;
            case COUNT_DISTINCT_EXACT: {
                Set<Object> seen = new LinkedHashSet<>();
                for (Map<String, Object> row : groupRows) {
                    Object v = row.get(field);
                    if (v != null) seen.add(normalizeNumber(v));
                }
                out.put(spec.alias(), (long) seen.size());
                return;
            }
            case HLL_DISTINCT: {
                HllSketch sketch = new HllSketch(HllConfig.of(spec.precision(), spec.seed()));
                for (Map<String, Object> row : groupRows) {
                    Object v = row.get(field);
                    if (v != null) sketch.offerValue(v);
                }
                hllengine.hll.HllEstimate est = sketch.estimate();
                out.put(spec.alias(), est.estimatedCardinality());
                out.put(spec.alias() + "_raw", est.rawEstimate());
                out.put(spec.errorAlias(), est.relativeStandardError());
                return;
            }
            case SUM: {
                double sum = 0;
                boolean any = false;
                for (Map<String, Object> row : groupRows) {
                    Object v = row.get(field);
                    if (v instanceof Number) {
                        sum += ((Number) v).doubleValue();
                        any = true;
                    }
                }
                out.put(spec.alias(), any ? sum : null);
                return;
            }
            case AVG: {
                double sum = 0;
                long n = 0;
                for (Map<String, Object> row : groupRows) {
                    Object v = row.get(field);
                    if (v instanceof Number) {
                        sum += ((Number) v).doubleValue();
                        n++;
                    }
                }
                out.put(spec.alias(), n == 0 ? null : sum / n);
                return;
            }
            case MIN:
            case MAX: {
                Object best = null;
                boolean min = spec.kind() == AggSpec.Kind.MIN;
                for (Map<String, Object> row : groupRows) {
                    Object v = row.get(field);
                    if (v == null) continue;
                    if (best == null) {
                        best = normalizeNumber(v);
                    } else {
                        int cmp = Values.compare(v, best);
                        if ((min && cmp < 0) || (!min && cmp > 0)) best = v;
                    }
                }
                out.put(spec.alias(), best);
                return;
            }
        }
    }

    private static Object normalizeNumber(Object v) {
        // Make 1 (long) and 1.0 (double) the same key for exact distinct counts.
        if (v instanceof Number && !(v instanceof Double) && !(v instanceof Long)) {
            return ((Number) v).longValue();
        }
        return v;
    }

    private void validateColumns(List<String> names, Dataset ds, String path) {
        // During parsing we do not know the dataset; this is called at execution
        // time, and only when a Dataset is available.
        if (ds == null) return;
        for (String name : names) {
            if (!ds.columns().contains(name)) {
                throw new ApiException(ApiException.BAD_REQUEST,
                        path + " references unknown column \"" + name
                                + "\" in dataset \"" + ds.name() + "\"");
            }
        }
    }

    private <T extends Node> T find(Class<T> type) {
        for (Node n : nodes) {
            if (type.isInstance(n)) return type.cast(n);
        }
        return null;
    }
}
