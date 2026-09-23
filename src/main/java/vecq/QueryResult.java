package vecq;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 一次查询的完整结果：被测向量化引擎的产物 + 与逐行参照的一致性标记 + 执行统计。
 * 同时负责组装对外 JSON 响应。
 */
public final class QueryResult {

    private final QueryPlan plan;
    private final QueryEngine.EngineRun vector;
    private final QueryEngine.EngineRun reference;
    private final ExecStats vectorStats;
    private final ExecStats referenceStats;

    QueryResult(QueryPlan plan,
                QueryEngine.EngineRun vector,
                QueryEngine.EngineRun reference,
                ExecStats vectorStats,
                ExecStats referenceStats) {
        this.plan = plan;
        this.vector = vector;
        this.reference = reference;
        this.vectorStats = vectorStats;
        this.referenceStats = referenceStats;
    }

    public QueryPlan plan() { return plan; }
    public SelectionVector selectionVector() { return vector.sv(); }

    /** 组装 /query 响应（同时也是 CLI 的输出内容）。 */
    public Map<String, Object> toResponseJson() {
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("ok", true);
        resp.put("table", plan.table().name());
        resp.put("batchSize", plan.batchSize());

        // 选择向量（过滤后的行下标，有序，可能重复）
        int[] sv = vector.sv().toArray();
        List<Object> svList = new ArrayList<>(sv.length);
        for (int i : sv) svList.add(i);
        resp.put("selectedRows", svList);
        resp.put("selectedCount", sv.length);

        // 投影（列式）
        if (vector.projection() != null) {
            Map<String, Object> proj = new LinkedHashMap<>();
            proj.put("format", "columnar");
            proj.put("rowCount", sv.length);
            Map<String, Object> cols = new LinkedHashMap<>();
            for (Map.Entry<String, List<Object>> e : vector.projection().entrySet()) {
                cols.put(e.getKey(), new ArrayList<>(e.getValue()));
            }
            proj.put("columns", cols);
            resp.put("projection", proj);
        }

        // 聚合
        if (vector.agg() != null) {
            resp.put("aggregates", QueryEngine.aggToJson(vector.agg()));
        }

        // 计划（可导出的执行计划）
        resp.put("plan", plan.planToJson());

        // 执行统计 + 与逐行参照一致性
        Map<String, Object> exec = new LinkedHashMap<>();
        exec.put("vector", statsJson(vectorStats));
        exec.put("rowInterpreter", statsJson(referenceStats));
        exec.put("enginesAgree", true);
        resp.put("execution", exec);
        return resp;
    }

    private Map<String, Object> statsJson(ExecStats s) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("filterBatches", s.filterBatches);
        m.put("downstreamBatches", s.downstreamBatches);
        return m;
    }
}
