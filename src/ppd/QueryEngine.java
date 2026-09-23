package ppd;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 查询引擎门面：接收 JSON 请求 -> 解析表与计划 -> 下推改写 -> 执行，
 * 返回含改写理由、前后计划与结果的 JSON 响应。
 *
 * <p>请求格式见 samples/。核心运算全部由本包自行实现，不依赖任何 SQL 引擎。</p>
 */
public class QueryEngine {

    public record Response(boolean ok, Object payload, String error) {
        public Map<String, Object> toJson() {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("ok", ok);
            if (ok) m.put("result", payload);
            else m.put("error", error);
            return m;
        }
    }

    public Response runJson(String requestJson) {
        try {
            Map<String, Object> req = Json.parseObject(requestJson);
            return new Response(true, run(req), null);
        } catch (EngineException ex) {
            return new Response(false, null, ex.getMessage());
        } catch (Exception ex) {
            return new Response(false, null,
                    ex.getClass().getSimpleName() + ": " + ex.getMessage());
        }
    }

    public Map<String, Object> run(Map<String, Object> req) {
        // 1. 装载基表（name 与 alias 都登记，使 scan.table 写哪个都能找到）
        Map<String, Table> byScanName = new LinkedHashMap<>();
        Map<String, Table> byQualifier = new LinkedHashMap<>();
        for (Map<String, Object> td : Json.getObjList(req, "tables")) {
            Table t = Table.fromJson(td);
            byScanName.put(Json.getStr(td, "name"), t);
            if (Json.getStr(td, "alias") != null) {
                byScanName.put(Json.getStr(td, "alias"), t);
            }
            byQualifier.put(t.qualifier(), t);
        }

        // 2. 解析原始计划
        Map<String, Object> planJson = Json.getObj(req, "plan");
        if (planJson == null) throw new EngineException("缺少 plan 字段");
        Plan original = Plan.fromJson(planJson, byScanName);

        // 3. 谓词下推改写
        PushdownRewriter rewriter = new PushdownRewriter();
        PushdownRewriter.RewriteResult rr = rewriter.rewrite(original);

        // 4. 执行改写前 / 改写后
        Executor exec = new Executor();
        List<Row> rowsBefore = exec.execute(original);
        List<Row> rowsAfter = exec.execute(rr.plan());
        Schema out = rr.plan().schema();

        // 5. 组装响应
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("equivalent", bagEquals(rowsBefore, rowsAfter));
        result.put("rowCountBefore", rowsBefore.size());
        result.put("rowCountAfter", rowsAfter.size());

        result.put("planBefore", PlanExplain.toJson(original));
        result.put("planAfter", PlanExplain.toJson(rr.plan()));
        result.put("planBeforeText", PlanExplain.toText(original).stripTrailing());
        result.put("planAfterText", PlanExplain.toText(rr.plan()).stripTrailing());

        List<Map<String, Object>> decisions = new ArrayList<>();
        for (PushdownRewriter.Decision d : rr.decisions()) decisions.add(d.toJson());
        result.put("rewriteDecisions", decisions);

        result.put("outputColumns", out.display());
        result.put("rowsBefore", rowsToJson(rowsBefore, out));
        result.put("rowsAfter", rowsToJson(rowsAfter, out));

        boolean exportData = Json.getBool(req, "exportData", true);
        if (exportData) {
            result.put("dataExport", exportData(byQualifier, original, rr, rowsBefore, rowsAfter));
        }
        return result;
    }

    private Map<String, Object> exportData(Map<String, Table> tables,
                                           Plan original,
                                           PushdownRewriter.RewriteResult rr,
                                           List<Row> before, List<Row> after) {
        Map<String, Object> exp = new LinkedHashMap<>();
        // 数据
        List<Map<String, Object>> ts = new ArrayList<>();
        for (Table t : tables.values()) {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("name", t.qualifier());
            m.put("columns", t.columnNames());
            List<Object> rows = new ArrayList<>();
            for (Row r : t.rows()) rows.add(r.toList());
            m.put("rows", rows);
            ts.add(m);
        }
        exp.put("tables", ts);
        // 执行计划
        exp.put("planBefore", PlanExplain.toJson(original));
        exp.put("planAfter", PlanExplain.toJson(rr.plan()));
        return exp;
    }

    private List<Map<String, Object>> rowsToJson(List<Row> rows, Schema schema) {
        List<Map<String, Object>> out = new ArrayList<>();
        for (Row r : rows) out.add(r.toNamedMap(schema));
        return out;
    }

    /** 包语义比较（多重集相等）。 */
    public static boolean bagEquals(List<Row> a, List<Row> b) {
        if (a.size() != b.size()) return false;
        List<Row> copy = new ArrayList<>(b);
        for (Row r : a) {
            int idx = copy.indexOf(r);
            if (idx < 0) return false;
            copy.remove(idx);
        }
        return true;
    }
}
