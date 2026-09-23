package tvl;

import java.util.List;
import java.util.Map;

import tvl.json.Json;

/**
 * JSON 请求入口的端到端测试：
 *  - 正常查询：逐行三值统计、选中行、执行计划
 *  - 错误响应：编译期错误携带位置
 *  - 数据与计划导出：响应中的 ast/plan/table 均可 JSON 序列化
 */
public class JsonRequestTest {

    public static void run() throws Exception {
        happyPath();
        nullRowsExcludedAsUnknown();
        shortCircuitAcrossRows();
        compileErrorCarriesPosition();
        badRequests();
        tableExportRoundTrip();
        astAndPlanExport();
        standaloneExpressionMode();
        divideByZeroRequestFailsWholeQuery();
        stringsAndBooleans();
    }

    private static String basicRequest() {
        return "{\n"
                + "  \"expression\": \"x > 1 AND y IS NOT NULL\",\n"
                + "  \"table\": {\n"
                + "    \"name\": \"nums\",\n"
                + "    \"columns\": [\n"
                + "      {\"name\": \"x\", \"type\": \"INTEGER\"},\n"
                + "      {\"name\": \"y\", \"type\": \"INTEGER\"}\n"
                + "    ],\n"
                + "    \"rows\": [\n"
                + "      [2, 10],\n"
                + "      [1, 10],\n"
                + "      [5, null],\n"
                + "      [null, 3],\n"
                + "      [9, 0]\n"
                + "    ]\n"
                + "  }\n"
                + "}";
    }

    private static void happyPath() {
        Map<String, Object> resp = Harness.request(basicRequest());
        TF.assertEquals(Boolean.TRUE, resp.get("ok"));

        Map<?, ?> result = (Map<?, ?>) resp.get("result");
        // x>1 AND y IS NOT NULL：
        //  [2,10]  T,T -> TRUE（选）
        //  [1,10]  F,. -> FALSE
        //  [5,null] T,F -> FALSE
        //  [null,3] U,T -> UNKNOWN
        //  [9,0]   T,T -> TRUE（选）
        TF.assertEquals(2, result.get("selectedCount"));
        @SuppressWarnings("unchecked")
        List<List<Object>> selected = (List<List<Object>>) result.get("selectedRows");
        TF.assertEquals(2L, selected.get(0).get(0));
        TF.assertEquals(9L, selected.get(1).get(0));

        Map<?, ?> stats = (Map<?, ?>) result.get("triStats");
        TF.assertEquals(2, stats.get("TRUE"));
        TF.assertEquals(2, stats.get("FALSE"));
        TF.assertEquals(1, stats.get("UNKNOWN"));
    }

    /** WHERE 三值语义：UNKNOWN 行不被选中，与 FALSE 同等待遇。 */
    private static void nullRowsExcludedAsUnknown() {
        String req = "{"
                + "\"expression\":\"a = 1\","
                + "\"table\":{\"columns\":[{\"name\":\"a\",\"type\":\"INTEGER\"}],"
                + "\"rows\":[[1],[2],[null]]}}";
        Map<String, Object> resp = Harness.request(req);
        Map<?, ?> result = (Map<?, ?>) resp.get("result");
        TF.assertEquals(1, result.get("selectedCount"));
        Map<?, ?> stats = (Map<?, ?>) result.get("triStats");
        TF.assertEquals(1, stats.get("TRUE"));
        TF.assertEquals(1, stats.get("FALSE"));
        TF.assertEquals(1, stats.get("UNKNOWN"));
    }

    /** 逐行求值：某些行短路安全，另一些行会触发除零，失败会整体报错。 */
    private static void shortCircuitAcrossRows() {
        // flag=TRUE 的行 OR 短路；其余行安全（den 非零）
        String safe = "{"
                + "\"expression\":\"flag OR (10 / den = 2)\","
                + "\"table\":{\"columns\":["
                + "{\"name\":\"flag\",\"type\":\"BOOLEAN\"},"
                + "{\"name\":\"den\",\"type\":\"INTEGER\"}],"
                + "\"rows\":[[true,0],[false,5],[false,10]]}}";
        Map<String, Object> resp = Harness.request(safe);
        TF.assertEquals(Boolean.TRUE, resp.get("ok"));
        Map<?, ?> result = (Map<?, ?>) resp.get("result");
        // [true,0]  OR 短路 -> TRUE
        // [false,5] 10/5=2 -> TRUE
        // [false,10] 10/10=1 -> FALSE
        TF.assertEquals(2, result.get("selectedCount"));
    }

    private static void compileErrorCarriesPosition() {
        String req = "{"
                + "\"expression\":\"x + 'oops'\","
                + "\"table\":{\"columns\":[{\"name\":\"x\",\"type\":\"INTEGER\"}],"
                + "\"rows\":[]}}";
        Map<String, Object> resp = Harness.request(req);
        TF.assertEquals(Boolean.FALSE, resp.get("ok"));
        Map<?, ?> err = (Map<?, ?>) resp.get("error");
        TF.assertEquals("TYPE_MISMATCH", err.get("code"));
        Map<?, ?> pos = (Map<?, ?>) err.get("position");
        TF.assertNotNull(pos, "类型错误应包含 position");
        // x + 'oops'，'+' 位于 offset 2
        TF.assertEquals(2, pos.get("offset"));
        TF.assertEquals(1, pos.get("line"));
        TF.assertEquals(3, pos.get("column"));

        // 未知列
        String req2 = "{\"expression\":\"missing = 1\",\"table\":{"
                + "\"columns\":[{\"name\":\"x\",\"type\":\"INTEGER\"}],\"rows\":[]}}";
        Map<String, Object> resp2 = Harness.request(req2);
        Map<?, ?> err2 = (Map<?, ?>) resp2.get("error");
        TF.assertEquals("UNKNOWN_COLUMN", err2.get("code"));
        TF.assertEquals(0, ((Map<?, ?>) err2.get("position")).get("offset"));
    }

    private static void badRequests() {
        // 非法 JSON
        Map<String, Object> r1 = Harness.request("{not json");
        TF.assertEquals("INVALID_JSON", ((Map<?, ?>) r1.get("error")).get("code"));

        // 缺少 expression
        Map<String, Object> r2 = Harness.request("{\"table\":{\"columns\":[]}}");
        TF.assertEquals("BAD_REQUEST", ((Map<?, ?>) r2.get("error")).get("code"));

        // 行长度不匹配
        String r3src = "{\"expression\":\"TRUE\",\"table\":{"
                + "\"columns\":[{\"name\":\"a\",\"type\":\"INTEGER\"}],"
                + "\"rows\":[[1,2]]}}";
        Map<String, Object> r3 = Harness.request(r3src);
        TF.assertEquals("BAD_REQUEST", ((Map<?, ?>) r3.get("error")).get("code"));

        // INTEGER 列收到字符串
        String r4src = "{\"expression\":\"TRUE\",\"table\":{"
                + "\"columns\":[{\"name\":\"a\",\"type\":\"INTEGER\"}],"
                + "\"rows\":[[\"no\"]]}}";
        Map<String, Object> r4 = Harness.request(r4src);
        TF.assertEquals("BAD_REQUEST", ((Map<?, ?>) r4.get("error")).get("code"));

        // 不支持的列类型
        String r5src = "{\"expression\":\"TRUE\",\"table\":{"
                + "\"columns\":[{\"name\":\"a\",\"type\":\"FLOAT\"}],\"rows\":[]}}";
        Map<String, Object> r5 = Harness.request(r5src);
        TF.assertEquals("BAD_REQUEST", ((Map<?, ?>) r5.get("error")).get("code"));

        // 重复列名
        String r6src = "{\"expression\":\"TRUE\",\"table\":{"
                + "\"columns\":[{\"name\":\"a\",\"type\":\"INTEGER\"},"
                + "{\"name\":\"a\",\"type\":\"INTEGER\"}],\"rows\":[]}}";
        Map<String, Object> r6 = Harness.request(r6src);
        TF.assertEquals("BAD_REQUEST", ((Map<?, ?>) r6.get("error")).get("code"));
    }

    /** includeTable=true 时响应携带完整数据；导出内容可再次被 JSON 解析。 */
    private static void tableExportRoundTrip() {
        String req = "{"
                + "\"expression\":\"x > 1\","
                + "\"includeTable\":true,"
                + "\"table\":{\"name\":\"nums\","
                + "\"columns\":[{\"name\":\"x\",\"type\":\"INTEGER\"}],"
                + "\"rows\":[[1],[null],[3]]}}";
        Map<String, Object> resp = Harness.request(req);
        Map<?, ?> table = (Map<?, ?>) resp.get("table");
        TF.assertEquals("nums", table.get("name"));
        TF.assertEquals(3, table.get("rowCount"));
        @SuppressWarnings("unchecked")
        List<List<Object>> rows = (List<List<Object>>) table.get("rows");
        TF.assertNull(rows.get(1).get(0), "导出表中的 NULL 应为 JSON null");
        // 整个响应可序列化为 JSON 并再解析（可导出保证）
        String serialized = Json.stringify(resp);
        Map<String, Object> reparsed = Json.parseObject(serialized);
        TF.assertEquals(Boolean.TRUE, reparsed.get("ok"));
    }

    private static void astAndPlanExport() {
        Map<String, Object> resp = Harness.request(basicRequest());
        Map<?, ?> plan = (Map<?, ?>) resp.get("plan");
        TF.assertEquals("Filter", plan.get("operator"));
        Map<?, ?> pred = (Map<?, ?>) predOf(plan);
        TF.assertEquals("Binary", pred.get("node"));
        TF.assertEquals("AND", pred.get("op"));
        Map<?, ?> scan = (Map<?, ?>) plan.get("input");
        TF.assertEquals("TableScan", scan.get("operator"));
        TF.assertEquals("nums", scan.get("table"));
        TF.assertEquals(5, scan.get("estimatedRows"));
        // columns 必须是真正的 JSON 字符串数组（而非 Java String[].toString()）
        @SuppressWarnings("unchecked")
        List<String> planCols = (List<String>) scan.get("columns");
        TF.assertEquals(2, planCols.size());
        TF.assertEquals("x", planCols.get(0));
        TF.assertEquals("y", planCols.get(1));

        // AST 节点带类型与位置
        Map<?, ?> ast = (Map<?, ?>) resp.get("ast");
        TF.assertEquals("BOOLEAN", ast.get("resolvedType"));
    }

    private static Map<?, ?> predOf(Map<?, ?> plan) {
        return (Map<?, ?>) plan.get("predicate");
    }

    private static void standaloneExpressionMode() {
        Map<String, Object> resp = Harness.request(
                "{\"expression\":\"NOT (TRUE AND FALSE) OR NULL\"}");
        TF.assertEquals(Boolean.TRUE, resp.get("ok"));
        Map<?, ?> result = (Map<?, ?>) resp.get("result");
        TF.assertEquals("standaloneExpression", result.get("mode"));
        Map<?, ?> value = (Map<?, ?>) result.get("value");
        TF.assertEquals("BOOLEAN", value.get("type"));
        TF.assertEquals(Boolean.TRUE, value.get("value"));
        TF.assertEquals("TRUE", result.get("triValue"));

        // 纯算术表达式返回整数值，不产生 triValue
        Map<String, Object> r2 = Harness.request("{\"expression\":\"6 * 7\"}");
        Map<?, ?> v2 = (Map<?, ?>) ((Map<?, ?>) r2.get("result")).get("value");
        TF.assertEquals(42L, v2.get("value"));
    }

    /** 求值期错误：任一行除零 -> 整个请求失败并返回错误码。 */
    private static void divideByZeroRequestFailsWholeQuery() {
        String req = "{\"expression\":\"10 / d = 1\",\"table\":{"
                + "\"columns\":[{\"name\":\"d\",\"type\":\"INTEGER\"}],"
                + "\"rows\":[[5],[0]]}}";
        Map<String, Object> resp = Harness.request(req);
        TF.assertEquals(Boolean.FALSE, resp.get("ok"));
        TF.assertEquals("DIVISION_BY_ZERO",
                ((Map<?, ?>) resp.get("error")).get("code"));
    }

    private static void stringsAndBooleans() {
        String req = "{\"expression\":\"name >= 'c' AND active\","
                + "\"table\":{\"columns\":["
                + "{\"name\":\"name\",\"type\":\"STRING\"},"
                + "{\"name\":\"active\",\"type\":\"BOOLEAN\"}],"
                + "\"rows\":[[\"alice\",true],[\"dave\",true],[\"zoe\",false],[null,true]]}}";
        Map<String, Object> resp = Harness.request(req);
        TF.assertEquals(Boolean.TRUE, resp.get("ok"));
        Map<?, ?> result = (Map<?, ?>) resp.get("result");
        // alice>=c FALSE; dave TRUE&TRUE 选; zoe TRUE&FALSE 不选; null->UNKNOWN
        TF.assertEquals(1, result.get("selectedCount"));
        Map<?, ?> stats = (Map<?, ?>) result.get("triStats");
        TF.assertEquals(1, stats.get("TRUE"));
        TF.assertEquals(2, stats.get("FALSE"));
        TF.assertEquals(1, stats.get("UNKNOWN"));
    }
}
