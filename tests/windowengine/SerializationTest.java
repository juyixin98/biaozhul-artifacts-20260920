package windowengine;

import windowengine.engine.WindowEngine;
import windowengine.plan.QueryPlan;

import java.nio.file.Path;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * JSON 序列化往返 + 导出目录测试：
 * - 输出关系导出后可直接作为 data.file 回读并再次执行，结果一致；
 * - 导出的 request.json 是可再次提交的规范化请求；
 * - 中文/转义字符的 JSON 往返不丢字。
 */
public final class SerializationTest {

    private Map<String, Object> sampleRequest() {
        Map<String, Object> data = new LinkedHashMap<>();
        data.put("columns", List.of(
                Map.of("name", "dept", "type", "STRING"),
                Map.of("name", "v", "type", "LONG")));
        data.put("rows", List.of(
                List.of("研发", 10L),
                java.util.Arrays.asList("销售", null),
                List.of("研发", -5L)));

        Map<String, Object> win = new LinkedHashMap<>();
        win.put("partitionBy", List.of("dept"));
        win.put("orderBy", List.of(Map.of("column", "v",
                "direction", "ASC", "nullOrder", "FIRST")));
        win.put("frame", "ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW");

        Map<String, Object> plan = new LinkedHashMap<>();
        plan.put("window", win);
        plan.put("functions", List.of(
                Map.of("function", "ROW_NUMBER", "alias", "rn"),
                Map.of("function", "RANK", "alias", "rk"),
                Map.of("function", "SUM", "column", "v", "alias", "cum")));

        Map<String, Object> req = new LinkedHashMap<>();
        req.put("data", data);
        req.put("plan", plan);
        return req;
    }

    public void testJsonRoundTripPreservesValues() {
        String text = Json.writePretty(sampleRequest());
        Object reparsed = Json.parse(text);
        String text2 = Json.writePretty(reparsed);
        Asserts.assertEquals(text, text2, "二次序列化应与首次完全一致（规范化）");
        Asserts.assertTrue(text.contains("研发"), "中文字符应原样保留");
    }

    public void testEscapeRoundTrip() {
        String tricky = "引号\"反斜杠\\换行\n制表\t";
        Map<String, Object> data = new LinkedHashMap<>();
        data.put("columns", List.of(Map.of("name", "s", "type", "STRING")));
        data.put("rows", List.of(List.of(tricky)));
        Map<String, Object> req = new LinkedHashMap<>();
        req.put("data", data);
        req.put("plan", Map.of("functions",
                List.of(Map.of("function", "ROW_NUMBER", "alias", "rn"))));
        String json = Json.write(req);
        QueryRequest parsed = new RequestParser().parse(Json.parse(json));
        Asserts.assertEquals(tricky, parsed.data().rows().get(0).get(0).asString(),
                "转义字符往返");
    }

    public void testExportedDataIsReusableAsInputFile() throws Exception {
        Path dir = Path.of("build", "test-export-roundtrip");
        deleteDir(dir);

        Map<String, Object> reqMap = sampleRequest();
        QueryRequest first = new RequestParser().parse(reqMap);
        Relation result1 = new WindowEngine().execute(first);
        var files = new Exporter().export(first, result1, dir.toString());

        // 1) 导出的规范化 request.json 保存的是原始输入数据，可原样再次提交并复现结果
        Object rerunNode = Json.parseFile(files.requestFile());
        QueryRequest rerun = new RequestParser().parse(rerunNode);
        Relation rerunResult = new WindowEngine().execute(rerun);
        Asserts.assertEquals(result1.rowCount(), rerunResult.rowCount(), "重放请求行数");
        Asserts.assertEquals(result1.schema().size(), rerunResult.schema().size(),
                "重放请求列数与首次结果一致（原始 2 列 + 3 窗口列）");
        for (int r = 0; r < result1.rowCount(); r++) {
            for (int c = 0; c < result1.schema().size(); c++) {
                Asserts.assertTrue(
                        result1.rows().get(r).get(c).valueEquals(rerunResult.rows().get(r).get(c)),
                        "重放结果逐格一致 行 " + r + " 列 " + c);
            }
        }

        // 2) 导出的 data.json 作为外部数据源，配一个只输出新别名的计划（不与导出列撞名）
        Map<String, Object> win2 = new LinkedHashMap<>();
        win2.put("partitionBy", List.of("dept"));
        win2.put("orderBy", List.of(Map.of("column", "v",
                "direction", "ASC", "nullOrder", "FIRST")));
        win2.put("frame", "ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW");
        Map<String, Object> plan2 = new LinkedHashMap<>();
        plan2.put("window", win2);
        plan2.put("functions", List.of(
                Map.of("function", "ROW_NUMBER", "alias", "rn2")));
        Map<String, Object> dataRef = new LinkedHashMap<>();
        dataRef.put("file", files.dataFile().toString());
        Map<String, Object> req2 = new LinkedHashMap<>();
        req2.put("data", dataRef);
        req2.put("plan", plan2);
        QueryRequest second = new RequestParser().parse(req2);
        Relation result2 = new WindowEngine().execute(second);

        Asserts.assertEquals(result1.rowCount(), result2.rowCount(), "回读行数");
        // 原列 + 三个旧输出列 + 新列
        Asserts.assertEquals(result1.schema().size() + 1, result2.schema().size(),
                "回读列数 = 原结果列 + 新窗口列");

        // 3) 回读后的原始输入列数据与首次结果逐格一致
        for (int r = 0; r < result1.rowCount(); r++) {
            for (int c = 0; c < 2; c++) { // dept, v 两个原始列
                Value a = result1.rows().get(r).get(c);
                Value b = result2.rows().get(r).get(c);
                Asserts.assertTrue(a.valueEquals(b),
                        "回读原始列一致 行 " + r + " 列 " + c);
            }
            // 旧导出的 rn 列也应无损保留
            int oldRn = result1.schema().requireIndex("rn");
            int keptRn = result2.schema().requireIndex("rn");
            Asserts.assertEquals(
                    result1.rows().get(r).get(oldRn).asLong(),
                    result2.rows().get(r).get(keptRn).asLong(),
                    "导出的窗口列 rn 回读无损");
        }

        deleteDir(dir);
    }

    public void testPlanExportExpandsDefaults() throws Exception {
        // 不给 frame、不给 nullOrder、不给 direction，导出应全部显式展开
        Map<String, Object> data = new LinkedHashMap<>();
        data.put("columns", List.of("v"));
        data.put("rows", List.of(List.of(1L)));
        Map<String, Object> req = new LinkedHashMap<>();
        req.put("data", data);
        req.put("plan", Map.of(
                "window", Map.of("orderBy", List.of("v")),
                "functions", List.of(Map.of("function", "SUM", "column", "v", "alias", "s"))));
        QueryRequest qr = new RequestParser().parse(req);
        Object planJson = JsonCodec.planToJson(qr.plan());
        String text = Json.writePretty(planJson);
        Asserts.assertTrue(text.contains("UNBOUNDED_PRECEDING"), "默认帧起点被展开");
        Asserts.assertTrue(text.contains("CURRENT_ROW"), "默认帧终点被展开");
        Asserts.assertTrue(text.contains("sqlText"), "帧导出带可读 SQL 文本");

        // 解析回来按结构断言，不依赖美化分隔符
        @SuppressWarnings("unchecked")
        Map<String, Object> pj = (Map<String, Object>) planJson;
        @SuppressWarnings("unchecked")
        Map<String, Object> win = (Map<String, Object>) pj.get("window");
        @SuppressWarnings("unchecked")
        List<Object> ob = (List<Object>) win.get("orderBy");
        @SuppressWarnings("unchecked")
        Map<String, Object> key = (Map<String, Object>) ob.get(0);
        Asserts.assertEquals("ASC", key.get("direction"), "默认方向 ASC 被展开");
        Asserts.assertEquals("FIRST", key.get("nullOrder"), "默认 NULL 顺序 NULLS FIRST 被展开");
        Asserts.assertEquals(Boolean.TRUE, key.get("nullOrderIsDefault"),
                "标注为默认 NULL 顺序");
    }

    @SuppressWarnings("unchecked")
    public void testOutputColumnOrdering() {
        Map<String, Object> req = sampleRequest();
        QueryRequest qr = new RequestParser().parse(req);
        Relation result = new WindowEngine().execute(qr);
        Asserts.assertEquals("dept", result.schema().name(0), "原列在前");
        Asserts.assertEquals("v", result.schema().name(1), "原列顺序保持");
        Asserts.assertEquals("rn", result.schema().name(2), "输出列按函数声明顺序");
        Asserts.assertEquals("rk", result.schema().name(3), "输出列按函数声明顺序");
        Asserts.assertEquals("cum", result.schema().name(4), "输出列按函数声明顺序");
    }

    private static void deleteDir(Path dir) throws Exception {
        if (!java.nio.file.Files.exists(dir)) {
            return;
        }
        try (var walk = java.nio.file.Files.walk(dir)) {
            walk.sorted(java.util.Comparator.reverseOrder())
                    .forEach(p -> {
                        try {
                            java.nio.file.Files.deleteIfExists(p);
                        } catch (Exception ignored) {
                            // 测试清理尽力而为
                        }
                    });
        }
    }
}
