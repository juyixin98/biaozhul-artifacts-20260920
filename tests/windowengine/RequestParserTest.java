package windowengine;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * RequestParser 语义校验与 JSON 解析测试：列不存在、重名、非法帧、
 * 类型不匹配、默认帧/默认 NULL 顺序展开、帧文本解析等。
 */
public final class RequestParserTest {

    private static List<Object> rowOf(Object... cells) {
        return new ArrayList<>(java.util.Arrays.asList(cells));
    }

    private static Map<String, Object> simpleRequest() {
        Map<String, Object> data = new LinkedHashMap<>();
        data.put("columns", List.of("a", "b")); // 裸列名，默认 LONG
        data.put("rows", List.of(rowOf(1L, 2L), rowOf(3L, null)));

        Map<String, Object> win = new LinkedHashMap<>();
        win.put("orderBy", List.of("a")); // 裸字符串排序键
        Map<String, Object> plan = new LinkedHashMap<>();
        plan.put("window", win);
        plan.put("functions", List.of(Map.of("function", "ROW_NUMBER", "alias", "rn")));

        Map<String, Object> req = new LinkedHashMap<>();
        req.put("data", data);
        req.put("plan", plan);
        return req;
    }

    private static QueryRequest parse(Map<String, Object> req) {
        return new RequestParser().parse(req);
    }

    private static EngineException expectError(Runnable action, String label) {
        try {
            action.run();
        } catch (EngineException e) {
            return e;
        }
        throw new AssertionError(label + "：应当抛出 EngineException");
    }

    public void testMinimalRequestParses() {
        QueryRequest qr = parse(simpleRequest());
        Asserts.assertEquals(2, qr.data().schema().size(), "2 个输入列");
        Asserts.assertEquals(Value.Type.LONG, qr.data().schema().type(1), "裸列默认 LONG");
        Asserts.assertEquals(1, qr.plan().functions().size(), "1 个函数");
        Asserts.assertEquals("rn", qr.plan().functions().get(0).alias(), "别名");
    }

    public void testDefaultNullOrderFollowsSqlStandard() {
        QueryRequest asc = parse(simpleRequest());
        Asserts.assertEquals(
                windowengine.plan.NullOrder.FIRST,
                asc.plan().window().orderBy().get(0).nullOrder(),
                "ASC 默认 NULLS FIRST");
        Asserts.assertEquals(
                windowengine.plan.NullOrder.FIRST,
                asc.plan().window().orderBy().get(0).nullOrder(),
                "裸排序键方向默认 ASC");

        Map<String, Object> req = simpleRequest();
        @SuppressWarnings("unchecked")
        Map<String, Object> win = (Map<String, Object>) req.get("plan");
        @SuppressWarnings("unchecked")
        Map<String, Object> w = (Map<String, Object>) win.get("window");
        w.put("orderBy", List.of(Map.of("column", "a", "direction", "DESC")));
        QueryRequest desc = parse(req);
        Asserts.assertEquals(
                windowengine.plan.NullOrder.LAST,
                desc.plan().window().orderBy().get(0).nullOrder(),
                "DESC 默认 NULLS LAST");
    }

    public void testDefaultFrameDependsOnOrderBy() {
        QueryRequest withOrder = parse(simpleRequest());
        Asserts.assertEquals(
                windowengine.plan.FrameBoundType.CURRENT_ROW,
                withOrder.plan().window().frame().end().type(),
                "有 ORDER BY 时默认终点 CURRENT ROW");
        Asserts.assertEquals(
                windowengine.plan.FrameBoundType.UNBOUNDED_PRECEDING,
                withOrder.plan().window().frame().start().type(),
                "有 ORDER BY 时默认起点 UNBOUNDED PRECEDING");

        Map<String, Object> req = simpleRequest();
        @SuppressWarnings("unchecked")
        Map<String, Object> p = (Map<String, Object>) req.get("plan");
        ((Map<?, ?>) p.get("window")).remove("orderBy");
        p.put("functions", List.of(sumDef("b", "s")));
        QueryRequest noOrder = parse(req);
        Asserts.assertEquals(
                windowengine.plan.FrameBoundType.UNBOUNDED_FOLLOWING,
                noOrder.plan().window().frame().end().type(),
                "无 ORDER BY 时默认帧延伸到分区末尾");
    }

    private static Map<String, Object> sumDef(String col, String alias) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("function", "SUM");
        m.put("column", col);
        m.put("alias", alias);
        return m;
    }

    public void testUnknownColumnRejected() {
        Map<String, Object> req = simpleRequest();
        @SuppressWarnings("unchecked")
        Map<String, Object> p = (Map<String, Object>) req.get("plan");
        @SuppressWarnings("unchecked")
        Map<String, Object> w = (Map<String, Object>) p.get("window");
        w.put("partitionBy", List.of("ghost"));
        EngineException e = expectError(() -> parse(req), "不存在的分区列");
        Asserts.assertEquals(ErrorCode.COLUMN_NOT_FOUND.code(), e.code(), "错误码");
    }

    public void testDuplicateInputColumnRejected() {
        Map<String, Object> req = simpleRequest();
        @SuppressWarnings("unchecked")
        Map<String, Object> d = (Map<String, Object>) req.get("data");
        d.put("columns", List.of("x", "X"));
        EngineException e = expectError(() -> parse(req), "大小写重复列");
        Asserts.assertEquals(ErrorCode.DUPLICATE_COLUMN.code(), e.code(), "重复列错误码");
    }

    public void testDuplicateAliasRejected() {
        Map<String, Object> req = simpleRequest();
        @SuppressWarnings("unchecked")
        Map<String, Object> p = (Map<String, Object>) req.get("plan");
        p.put("functions", List.of(
                Map.of("function", "ROW_NUMBER", "alias", "rn"),
                Map.of("function", "RANK", "alias", "RN")));
        EngineException e = expectError(() -> parse(req), "重复别名");
        Asserts.assertEquals(ErrorCode.DUPLICATE_COLUMN.code(), e.code(), "别名大小写不敏感去重");
    }

    public void testAliasClashesWithInputColumn() {
        Map<String, Object> req = simpleRequest();
        @SuppressWarnings("unchecked")
        Map<String, Object> p = (Map<String, Object>) req.get("plan");
        p.put("functions", List.of(Map.of("function", "ROW_NUMBER", "alias", "a")));
        EngineException e = expectError(() -> parse(req), "别名撞输入列");
        Asserts.assertEquals(ErrorCode.DUPLICATE_COLUMN.code(), e.code(), "错误码");
    }

    public void testRowWidthMismatchRejected() {
        Map<String, Object> req = simpleRequest();
        @SuppressWarnings("unchecked")
        Map<String, Object> d = (Map<String, Object>) req.get("data");
        d.put("rows", List.of(List.of(1L, 2L, 3L)));
        EngineException e = expectError(() -> parse(req), "列数不符");
        Asserts.assertEquals(ErrorCode.INVALID_REQUEST.code(), e.code(), "错误码");
    }

    public void testDeclaredTypeMismatchRejected() {
        Map<String, Object> data = new LinkedHashMap<>();
        data.put("columns", List.of(Map.of("name", "s", "type", "STRING")));
        data.put("rows", List.of(List.of(42L)));
        Map<String, Object> req = new LinkedHashMap<>();
        req.put("data", data);
        req.put("plan", simpleRequest().get("plan"));
        EngineException e = expectError(() -> parse(req), "STRING 列收到整数");
        Asserts.assertEquals(ErrorCode.TYPE_MISMATCH.code(), e.code(), "类型错误码");
    }

    public void testSumOnStringColumnRejected() {
        Map<String, Object> data = new LinkedHashMap<>();
        data.put("columns", List.of("s"));
        // 裸列名默认 LONG，这里用对象声明显式 STRING
        data.put("columns", List.of(Map.of("name", "s", "type", "STRING")));
        data.put("rows", List.of(List.of("x")));
        Map<String, Object> win = new LinkedHashMap<>();
        Map<String, Object> plan = new LinkedHashMap<>();
        plan.put("window", win);
        plan.put("functions", List.of(sumDef("s", "tot")));
        Map<String, Object> req = new LinkedHashMap<>();
        req.put("data", data);
        req.put("plan", plan);
        EngineException e = expectError(() -> parse(req), "SUM(STRING)");
        Asserts.assertEquals(ErrorCode.TYPE_MISMATCH.code(), e.code(), "SUM 参数必须 LONG");
    }

    public void testInvalidFrameRejected() {
        Map<String, Object> req = simpleRequest();
        @SuppressWarnings("unchecked")
        Map<String, Object> p = (Map<String, Object>) req.get("plan");
        @SuppressWarnings("unchecked")
        Map<String, Object> w = (Map<String, Object>) p.get("window");
        Map<String, Object> bad = new LinkedHashMap<>();
        bad.put("mode", "ROWS");
        bad.put("start", Map.of("type", "FOLLOWING", "offset", 2));
        bad.put("end", Map.of("type", "PRECEDING", "offset", 1));
        w.put("frame", bad);
        EngineException e = expectError(() -> parse(req), "起点晚于终点的帧");
        Asserts.assertEquals(ErrorCode.INVALID_FRAME.code(), e.code(), "非法帧错误码");
    }

    public void testRangeFrameUnsupported() {
        Map<String, Object> req = simpleRequest();
        @SuppressWarnings("unchecked")
        Map<String, Object> p = (Map<String, Object>) req.get("plan");
        @SuppressWarnings("unchecked")
        Map<String, Object> w = (Map<String, Object>) p.get("window");
        Map<String, Object> bad = new LinkedHashMap<>();
        bad.put("mode", "RANGE");
        bad.put("start", Map.of("type", "UNBOUNDED_PRECEDING"));
        bad.put("end", Map.of("type", "CURRENT_ROW"));
        w.put("frame", bad);
        EngineException e = expectError(() -> parse(req), "RANGE 帧");
        Asserts.assertEquals(ErrorCode.UNSUPPORTED.code(), e.code(), "RANGE 应报 UNSUPPORTED");
    }

    public void testFrameTextParsing() {
        RequestParser parser = new RequestParser();
        var f1 = parser.parseFrameText("ROWS BETWEEN 2 PRECEDING AND 3 FOLLOWING");
        Asserts.assertEquals(windowengine.plan.FrameBoundType.PRECEDING,
                f1.start().type(), "文本帧起点类型");
        Asserts.assertEquals(2L, f1.start().offset(), "文本帧起点 offset");
        Asserts.assertEquals(3L, f1.end().offset(), "文本帧终点 offset");

        var f2 = parser.parseFrameText("rows unbounded preceding");
        Asserts.assertEquals(windowengine.plan.FrameBoundType.UNBOUNDED_PRECEDING,
                f2.start().type(), "单边界帧起点");
        Asserts.assertEquals(windowengine.plan.FrameBoundType.CURRENT_ROW,
                f2.end().type(), "单边界帧终点隐含 CURRENT ROW");

        EngineException e = expectError(
                () -> parser.parseFrameText("ROWS BETWEEN 1 PRECEDING"),
                "缺 AND 的文本帧");
        Asserts.assertEquals(ErrorCode.INVALID_FRAME.code(), e.code(), "文本帧语法错误码");
    }

    public void testUnknownFunctionRejected() {
        Map<String, Object> req = simpleRequest();
        @SuppressWarnings("unchecked")
        Map<String, Object> p = (Map<String, Object>) req.get("plan");
        p.put("functions", List.of(Map.of("function", "DENSE_RANK", "alias", "dr")));
        EngineException e = expectError(() -> parse(req), "未实现函数");
        Asserts.assertEquals(ErrorCode.UNSUPPORTED.code(), e.code(), "不支持的函数");
    }

    public void testDecimalsRejectedByJsonParser() {
        String json = "{\"data\":{\"columns\":[\"a\"],\"rows\":[[1.5]]},"
                + "\"plan\":{\"functions\":[{\"function\":\"ROW_NUMBER\",\"alias\":\"rn\"}]}}";
        EngineException e = expectError(() -> new RequestParser().parse(Json.parse(json)),
                "小数");
        Asserts.assertEquals(ErrorCode.INVALID_JSON.code(), e.code(), "小数应被 JSON 层拒绝");
    }

    public void testMalformedJsonRejected() {
        EngineException e = expectError(() -> Json.parse("{oops"), "坏 JSON");
        Asserts.assertEquals(ErrorCode.INVALID_JSON.code(), e.code(), "坏 JSON 错误码");
    }

    public void testDefaultAliasesGenerated() {
        Map<String, Object> data = new LinkedHashMap<>();
        data.put("columns", List.of("a", "v"));
        data.put("rows", List.of());
        Map<String, Object> win = new LinkedHashMap<>();
        win.put("orderBy", List.of("a"));
        Map<String, Object> plan = new LinkedHashMap<>();
        plan.put("window", win);
        plan.put("functions", List.of(
                Map.of("function", "ROW_NUMBER"),
                Map.of("function", "SUM", "column", "v")));
        Map<String, Object> req = new LinkedHashMap<>();
        req.put("data", data);
        req.put("plan", plan);
        QueryRequest qr = parse(req);
        Asserts.assertEquals("row_number1", qr.plan().functions().get(0).alias(),
                "ROW_NUMBER 默认别名");
        Asserts.assertEquals("sum_v", qr.plan().functions().get(1).alias(),
                "SUM 默认别名");
    }
}
