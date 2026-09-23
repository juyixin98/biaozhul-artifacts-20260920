package tvl.test;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import tvl.api.JsonApi;
import tvl.json.Json;

/**
 * 端到端测试：通过 JSON 请求驱动 JsonApi，覆盖
 * 装载、查询、3VL 过滤行计数、select/limit、执行计划统计、导出与全部错误信封。
 */
public class EngineApiTest extends TestBase {

    private JsonApi api;

    @Override
    public String name() {
        return "Engine + JSON API end-to-end";
    }

    @Override
    public void run() {
        api = new JsonApi();
        loadTable();
        basicFilteringAnd3vl();
        selectLimitAndPlan();
        isNullFiltering();
        shortCircuitThroughApi();
        errorsThroughApi();
        exportAndRoundTrip();
        batch();
    }

    // ------------------------------------------------------------------ load

    private void loadTable() {
        // employees(id INTEGER, dept STRING, age INTEGER, active BOOLEAN, mgr_id INTEGER nullable)
        Map<String, Object> load = new LinkedHashMap<>();
        load.put("action", "load");
        load.put("table", "employees");
        load.put("columns", List.of(
                col("id", "INTEGER"),
                col("dept", "STRING"),
                col("age", "INTEGER"),
                col("active", "BOOLEAN"),
                col("mgr_id", "INTEGER")
        ));
        load.put("rows", List.of(
                row("id", 1, "dept", "eng", "age", 30, "active", true, "mgr_id", 10),
                row("id", 2, "dept", "eng", "age", 25, "active", false, "mgr_id", 10),
                row("id", 3, "dept", "sales", "age", 40, "active", true, "mgr_id", null),
                row("id", 4, "dept", "sales", "age", null, "active", true, "mgr_id", null),
                row("id", 5, "dept", "eng", "age", 50, "active", null, "mgr_id", 20)
        ));
        Map<String, Object> resp = api.handle(load);
        expectEq(true, resp.get("ok"), "load ok");
        expectEq(5, resp.get("rowsLoaded"), "load row count");

        // 装载期类型错误
        Map<String, Object> bad = new LinkedHashMap<>();
        bad.put("action", "load");
        bad.put("table", "bad");
        bad.put("columns", List.of(col("x", "INTEGER")));
        bad.put("rows", List.of(row("x", "not-a-number")));
        Map<String, Object> badResp = api.handle(bad);
        expectEq("TYPE_MISMATCH", badResp.get("error"), "load rejects wrong value type");
    }

    // -------------------------------------------------------------- filtering

    private void basicFilteringAnd3vl() {
        // dept='eng' 且 age > 27：行1(30)、行5(50) 命中；行2(25) FALSE 剔除
        // （eng 部门没有 age NULL 的行；age NULL 的是 sales 的 id=4）
        Map<String, Object> resp = query("dept = 'eng' AND age > 27", null, null);
        List<Long> ids = ids(resp);
        expectEq(List.of(1L, 5L), ids, "eng AND age>27: FALSE/UNKNOWN excluded");

        // age NULL 的行用 IS NULL 找回
        Map<String, Object> resp2 = query("age IS NULL", null, null);
        expectEq(List.of(4L), ids(resp2), "age IS NULL finds id 4");

        // NULL 的反向比较不命中：age <> 40 对 age NULL 是 UNKNOWN
        Map<String, Object> resp3 = query("age <> 40", null, null);
        expectEq(List.of(1L, 2L, 5L), ids(resp3),
                "age <> 40: NULL rows excluded (UNKNOWN), present rows 1,2,5");

        // OR 的 UNKNOWN 传播：sales 或 age > 60
        Map<String, Object> resp4 = query("dept = 'sales' OR age > 60", null, null);
        expectEq(List.of(3L, 4L), ids(resp4), "sales OR age>60: sales rows survive");

        // active 列本身为 NULL 的行：active = TRUE 为 UNKNOWN，剔除
        Map<String, Object> resp5 = query("active = TRUE", null, null);
        expectEq(List.of(1L, 3L, 4L), ids(resp5), "active = TRUE excludes NULL active (id5)");
        // IS DISTINCT 风格通过 IS NULL 表达：active IS NOT NULL
        Map<String, Object> resp6 = query("active IS NOT NULL", null, null);
        expectEq(List.of(1L, 2L, 3L, 4L), ids(resp6), "active IS NOT NULL excludes id5");
    }

    private void selectLimitAndPlan() {
        Map<String, Object> resp = query("id >= 0", List.of("id", "dept"), 2L);
        @SuppressWarnings("unchecked")
        List<Map<String, Object>> rows = (List<Map<String, Object>>) resp.get("rows");
        expectEq(2, rows.size(), "limit 2");
        check(rows.get(0).containsKey("id") && !rows.get(0).containsKey("age"),
                "projection keeps only requested columns");
        expectEq(List.of(1L, 2L), ids(resp), "limit ordering preserves scan order");

        @SuppressWarnings("unchecked")
        Map<String, Object> stats = (Map<String, Object>) resp.get("stats");
        expectEq(5L, stats.get("scanned"), "stats.scanned");
        expectEq(5L, stats.get("matched"), "stats.matched");
        expectEq(2L, stats.get("returned"), "stats.returned");

        @SuppressWarnings("unchecked")
        Map<String, Object> plan = (Map<String, Object>) resp.get("plan");
        expectEq("LIMIT", plan.get("node"), "root plan node LIMIT");
        expectEq(2L, plan.get("rowsOut"), "limit rowsOut");
        @SuppressWarnings("unchecked")
        Map<String, Object> project = (Map<String, Object>) ((List<?>) plan.get("children")).get(0);
        expectEq("PROJECT", project.get("node"), "child PROJECT");
    }

    private void isNullFiltering() {
        // mgr_id IS NULL 且 active
        Map<String, Object> resp = query("mgr_id IS NULL AND active = TRUE", null, null);
        expectEq(List.of(3L, 4L), ids(resp), "mgr null AND active");
        // mgr_id IS NOT NULL
        Map<String, Object> resp2 = query("mgr_id IS NOT NULL", null, null);
        expectEq(List.of(1L, 2L, 5L), ids(resp2), "mgr not null");
    }

    private void shortCircuitThroughApi() {
        // id = 1 为 FALSE 时短路，右侧除零不触发；加入恒假列条件
        Map<String, Object> resp = query("id = 999 AND (1 / (id - id) = 1)", null, null);
        expectEq(true, resp.get("ok"), "FALSE AND div0 short-circuits, returns ok");
        expectEq(List.of(), ids(resp), "no rows matched");

        // id 对所有行都不等于 999 -> 全部短路，无除零错误
        // 反过来，存在 id=1 的行使左侧 TRUE 时，右侧必须求值 -> 除零错误
        Map<String, Object> resp2 = queryRaw("id = 1 AND (1 / (id - id) = 1)");
        expectEq(false, resp2.get("ok"), "TRUE-side forces div0 evaluation -> error");
        expectEq("EVAL_ERROR", resp2.get("error"), "error code EVAL_ERROR");
        @SuppressWarnings("unchecked")
        Map<String, Object> detail = (Map<String, Object>) resp2.get("detail");
        check(detail != null && detail.containsKey("offset") && detail.containsKey("snippet"),
                "eval error carries offset and snippet");
    }

    // ------------------------------------------------------------------ errors

    private void errorsThroughApi() {
        expectEq("INVALID_JSON",
                ((Map<?, ?>) Json.parse(api.handleJson("{not json"))).get("error"),
                "invalid JSON envelope");

        expectEq("PARSE_ERROR", queryRaw("id ==").get("error"), "parse error via API");
        expectEq("TYPE_ERROR", queryRaw("id = 'x'").get("error"), "type error via API");
        expectEq("LEX_ERROR", queryRaw("id @@ 1").get("error"), "lex error via API");

        Map<String, Object> unknownTable = new LinkedHashMap<>();
        unknownTable.put("action", "query");
        unknownTable.put("table", "nope");
        expectEq("UNKNOWN_TABLE", api.handle(unknownTable).get("error"), "unknown table");

        Map<String, Object> unknownAction = new LinkedHashMap<>();
        unknownAction.put("action", "frobnicate");
        expectEq("INVALID_REQUEST", api.handle(unknownAction).get("error"), "unknown action");

        Map<String, Object> unknownColSelect = new LinkedHashMap<>();
        unknownColSelect.put("action", "query");
        unknownColSelect.put("table", "employees");
        unknownColSelect.put("select", List.of("nope"));
        expectEq("ENGINE_ERROR", api.handle(unknownColSelect).get("error"),
                "unknown projected column");
    }

    // ------------------------------------------------------------------ export

    private void exportAndRoundTrip() {
        Map<String, Object> exportReq = new LinkedHashMap<>();
        exportReq.put("action", "export");
        Map<String, Object> exported = api.handle(exportReq);
        expectEq(true, exported.get("ok"), "export ok");
        @SuppressWarnings("unchecked")
        List<Map<String, Object>> tables = (List<Map<String, Object>>) exported.get("tables");
        expectEq(1, tables.size(), "export one table");
        Map<String, Object> empTable = tables.get(0);
        expectEq("employees", empTable.get("name"), "export table name");
        expectEq(5, empTable.get("rowCount"), "export row count");

        // 导出的 NULL 仍然为 null
        @SuppressWarnings("unchecked")
        List<Map<String, Object>> rows =
                (List<Map<String, Object>>) empTable.get("rows");
        check(rows.get(2).get("mgr_id") == null, "exported null survives as JSON null");

        // 导出的内容可以重新装载（表改名）形成往返
        @SuppressWarnings("unchecked")
        List<Map<String, Object>> columns =
                (List<Map<String, Object>>) empTable.get("columns");
        Map<String, Object> reload = new LinkedHashMap<>();
        reload.put("action", "load");
        reload.put("table", "employees_copy");
        reload.put("columns", columns);
        reload.put("rows", rows);
        expectEq(true, api.handle(reload).get("ok"), "exported data reloads (round-trip)");
    }

    // ------------------------------------------------------------------- batch

    private void batch() {
        // 同一引擎实例内顺序执行 load -> query -> export，共享内存状态
        Map<String, Object> load = new LinkedHashMap<>();
        load.put("action", "load");
        load.put("table", "t2");
        load.put("columns", List.of(col("x", "INTEGER")));
        load.put("rows", List.of(row("x", 1), row("x", 2)));
        expectEq(true, api.handle(load).get("ok"), "batch-style load");
        Map<String, Object> q = new LinkedHashMap<>();
        q.put("action", "query");
        q.put("table", "t2");
        q.put("where", "x = 2");
        Map<String, Object> qr = api.handle(q);
        expectEq(1L, qr.get("rowCount"), "batch-style query shares state");
    }

    // ----------------------------------------------------------------- helpers

    private Map<String, Object> query(String where, List<String> select, Long limit) {
        Map<String, Object> req = new LinkedHashMap<>();
        req.put("action", "query");
        req.put("table", "employees");
        if (where != null) req.put("where", where);
        if (select != null) req.put("select", select);
        if (limit != null) req.put("limit", limit);
        Map<String, Object> resp = api.handle(req);
        if (!Boolean.TRUE.equals(resp.get("ok"))) {
            fail("query unexpectedly failed: " + resp);
        }
        return resp;
    }

    @SuppressWarnings("unchecked")
    private Map<String, Object> queryRaw(String where) {
        String json = "{\"action\":\"query\",\"table\":\"employees\",\"where\":\""
                + where.replace("\"", "\\\"") + "\"}";
        return (Map<String, Object>) Json.parse(api.handleJson(json));
    }

    @SuppressWarnings("unchecked")
    private List<Long> ids(Map<String, Object> queryResp) {
        List<Long> ids = new ArrayList<>();
        for (Object o : (List<?>) queryResp.get("rows")) {
            Map<String, Object> r = (Map<String, Object>) o;
            Object id = r.get("id");
            if (id != null) ids.add((Long) id);
        }
        return ids;
    }

    private static Map<String, Object> col(String name, String type) {
        Map<String, Object> c = new LinkedHashMap<>();
        c.put("name", name);
        c.put("type", type);
        return c;
    }

    private static Map<String, Object> row(Object... kv) {
        Map<String, Object> r = new LinkedHashMap<>();
        for (int i = 0; i < kv.length; i += 2) {
            r.put((String) kv[i], kv[i + 1]);
        }
        return r;
    }
}
