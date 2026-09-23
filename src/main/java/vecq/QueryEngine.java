package vecq;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 查询引擎门面：对同一计划分别跑
 *   1) 向量化批执行 {@link BatchEngine}（被验收对象）；
 *   2) 逐行解释器 {@link RowInterpreter}（参照），
 * 并比较两者的选择向量、投影与聚合结果。不一致时抛 {@link EngineMismatchException}。
 */
public final class QueryEngine {

    private final Catalog catalog;

    public QueryEngine(Catalog catalog) {
        this.catalog = catalog;
    }

    public Catalog catalog() {
        return catalog;
    }

    public QueryResult execute(Object request) {
        QueryPlan plan = new PlanBuilder(catalog).build(request);
        return executePlan(plan);
    }

    public QueryResult executePlan(QueryPlan plan) {
        // 向量化（被测）
        ExecStats vs = new ExecStats();
        EngineRun vector = runVector(plan, vs);

        // 逐行参照
        ExecStats rs = new ExecStats();
        EngineRun reference = runReference(plan, rs);

        // 差分比较
        compare(vector, reference);

        return new QueryResult(plan, vector, reference, vs, rs);
    }

    private EngineRun runVector(QueryPlan plan, ExecStats stats) {
        SelectionVector input = plan.selection() != null
                ? plan.selection()
                : SelectionVector.lazyAll(plan.table().rowCount());
        SelectionVector sv = input;
        if (plan.filter() != null) {
            sv = new VectorFilter(plan.table()).apply(plan.filter(), input, plan.batchSize(), stats);
        }
        return finish(plan, sv, stats);
    }

    private EngineRun runReference(QueryPlan plan, ExecStats stats) {
        SelectionVector input = plan.selection() != null
                ? plan.selection()
                : SelectionVector.lazyAll(plan.table().rowCount());
        SelectionVector sv = input;
        if (plan.filter() != null) {
            sv = new RowInterpreter(plan.table()).apply(plan.filter(), input, stats);
        }
        return finish(plan, sv, stats);
    }

    private EngineRun finish(QueryPlan plan, SelectionVector sv, ExecStats stats) {
        ProjectAggregate pa = new ProjectAggregate(plan);
        Map<String, List<Object>> projection =
                plan.projection().isEmpty() ? null : pa.project(sv, stats);
        ProjectAggregate.AggResult agg =
                plan.hasAggregation() ? pa.aggregate(sv, stats) : null;
        return new EngineRun(sv, projection, agg, stats);
    }

    private void compare(EngineRun a, EngineRun b) {
        int[] sa = a.sv().toArray();
        int[] sb = b.sv().toArray();
        if (!java.util.Arrays.equals(sa, sb)) {
            throw new EngineMismatchException("选择向量不一致：\n  向量化 "
                    + java.util.Arrays.toString(sa) + "\n  逐行   "
                    + java.util.Arrays.toString(sb));
        }
        if (!java.util.Objects.deepEquals(a.projection(), b.projection())) {
            throw new EngineMismatchException("投影结果不一致：\n  向量化 "
                    + Json.write(a.projection()) + "\n  逐行   "
                    + Json.write(b.projection()));
        }
        if (!aggEqual(a.agg(), b.agg())) {
            throw new EngineMismatchException("聚合结果不一致：\n  向量化 "
                    + Json.write(aggToJson(a.agg())) + "\n  逐行   "
                    + Json.write(aggToJson(b.agg())));
        }
    }

    private static boolean aggEqual(ProjectAggregate.AggResult a, ProjectAggregate.AggResult b) {
        if (a == null || b == null) return a == b;
        return java.util.Objects.deepEquals(aggToJson(a), aggToJson(b));
    }

    static Map<String, Object> aggToJson(ProjectAggregate.AggResult agg) {
        Map<String, Object> m = new LinkedHashMap<>();
        if (agg.globalRow != null) {
            m.put("kind", "global");
            m.put("row", agg.globalRow);
        } else {
            m.put("kind", "grouped");
            m.put("groupColumns", agg.groupColumns);
            m.put("rows", agg.groupRows);
        }
        return m;
    }

    /** 单次引擎执行产物（内部）。 */
    record EngineRun(SelectionVector sv,
                     Map<String, List<Object>> projection,
                     ProjectAggregate.AggResult agg,
                     ExecStats stats) {}
}
