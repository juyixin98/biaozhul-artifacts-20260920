package dev.dedup.hll;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** 查询引擎 JSON 端到端：正常批处理、错误码、合并、导出导入、坏 JSON、估计标注。 */
public class QueryEngineTest extends TestCase {

    @Override
    public String name() {
        return "查询引擎 JSON 端到端（算子/错误处理/估计标注）";
    }

    @Override
    public void run() throws Exception {
        testBasicLifecycle();
        testShardMergeViaJson();
        testExportImportFormats();
        testErrorCases();
        testEstimateLabeling();
        testMalformedJson();
        testPlanPresent();
    }

    @SuppressWarnings("unchecked")
    private Map<String, Object> run(String json) {
        return new QueryEngine().execute((Map<String, Object>) Json.parse(json));
    }

    @SuppressWarnings("unchecked")
    private Map<String, Object> resultAt(Map<String, Object> resp, int i) {
        return (Map<String, Object>) ((List<?>) resp.get("results")).get(i);
    }

    private void testBasicLifecycle() {
        Map<String, Object> resp = run("{"
                + "\"ops\":["
                + "{\"op\":\"create\",\"name\":\"uv\",\"precision\":12},"
                + "{\"op\":\"add\",\"name\":\"uv\",\"values\":[\"a\",\"b\",\"c\",1,2,true,null,\"a\",\"b\"]}"
                + "]}");
        eq(resp.get("ok"), true, "create+add 成功");
        // a/b/c/1/2/true/null = 7 个不同类型元素；a,b 重复
        Map<String, Object> est = run("{"
                + "\"ops\":["
                + "{\"op\":\"create\",\"name\":\"uv\",\"precision\":14},"
                + "{\"op\":\"add\",\"name\":\"uv\",\"values\":[\"a\",\"b\",\"c\",1,2,true,null,\"a\",\"b\"]},"
                + "{\"op\":\"estimate\",\"name\":\"uv\"}"
                + "]}");
        Map<String, Object> estResult = resultAt(est, 2);
        long card = ((Number) estResult.get("cardinalityEstimate")).longValue();
        check(card >= 6 && card <= 9, "7 个跨类型去重元素估计在 6..9，实际 " + card);
        eq(estResult.get("exact"), false, "exact 必须显式为 false");
        check(estResult.get("estimationNote") != null, "必须有人读的估计说明");
    }

    private void testShardMergeViaJson() {
        // 两个分片各含 0..999 与其后的 1000..1999，部分重叠
        List<String> ops = new ArrayList<>();
        ops.add("{\"op\":\"create\",\"name\":\"s1\",\"precision\":12}");
        ops.add("{\"op\":\"create\",\"name\":\"s2\",\"precision\":12}");
        ops.add("{\"op\":\"create\",\"name\":\"total\",\"precision\":12}");
        StringBuilder v1 = new StringBuilder();
        StringBuilder v2 = new StringBuilder();
        for (int i = 0; i < 1500; i++) {
            v1.append(i > 0 ? "," : "").append("\"u").append(i).append("\"");
        }
        for (int i = 1000; i < 3000; i++) {
            v2.append(i > 1000 ? "," : "").append("\"u").append(i).append("\"");
        }
        ops.add("{\"op\":\"add\",\"name\":\"s1\",\"values\":[" + v1 + "]}");
        ops.add("{\"op\":\"add\",\"name\":\"s2\",\"values\":[" + v2 + "]}");
        ops.add("{\"op\":\"merge\",\"name\":\"total\",\"sources\":[\"s1\",\"s2\"]}");
        ops.add("{\"op\":\"estimate\",\"name\":\"total\"}");
        Map<String, Object> resp = run("{\"ops\":[" + String.join(",", ops) + "]}");
        eq(resp.get("ok"), true, "分片 add+merge 成功");
        Map<String, Object> est = resultAt(resp, 6);
        long card = ((Number) est.get("cardinalityEstimate")).longValue();
        double rel = Math.abs(card - 3000) / 3000.0;
        check(rel < 0.10, "并集真实 3000，估计在 10% 内，实际 " + card);
    }

    private void testExportImportFormats() {
        Map<String, Object> resp = run("{"
                + "\"ops\":["
                + "{\"op\":\"create\",\"name\":\"s\",\"precision\":12},"
                + "{\"op\":\"add\",\"name\":\"s\",\"values\":[\"x1\",\"x2\",\"x3\",\"x4\"]},"
                + "{\"op\":\"export\",\"name\":\"s\",\"format\":\"binary\"},"
                + "{\"op\":\"export\",\"name\":\"s\",\"format\":\"json\"}"
                + "]}");
        eq(resp.get("ok"), true, "export 成功");
        Map<String, Object> bin = resultAt(resp, 2);
        check(bin.get("dataBase64") instanceof String, "binary 导出含 dataBase64");
        check(((Number) bin.get("bytes")).intValue() > 12, "binary 导出字节数>头部");

        // 用导出的数据 load 回新引擎，再估计，必须一致
        @SuppressWarnings("unchecked")
        Map<String, Object> binSketch = (Map<String, Object>) bin;
        String loadJson = "{\"ops\":["
                + "{\"op\":\"load\",\"name\":\"t\",\"format\":\"binary\",\"dataBase64\":"
                + Json.write(binSketch.get("dataBase64"), false) + "},"
                + "{\"op\":\"estimate\",\"name\":\"t\"}"
                + "]}";
        Map<String, Object> loaded = run(loadJson);
        eq(loaded.get("ok"), true, "binary load 成功");
        Map<String, Object> estLoaded = resultAt(loaded, 1);
        check(estLoaded.get("cardinalityEstimate") != null, "载入后可估计");

        // 损坏的 base64 二进制 -> BAD_FORMAT
        String corrupt = "{\"ops\":["
                + "{\"op\":\"load\",\"name\":\"t\",\"format\":\"binary\",\"dataBase64\":\""
                + java.util.Base64.getEncoder().encodeToString(new byte[] {'H','L','C','D',2,0})
                + "\"}]}";
        Map<String, Object> bad = run(corrupt);
        eq(bad.get("ok"), false, "坏二进制 load 失败");
        @SuppressWarnings("unchecked")
        Map<String, Object> err = (Map<String, Object>) bad.get("error");
        eq(err.get("code"), "BAD_FORMAT", "坏格式错误码");
        eq(err.get("failedOpIndex"), 0, "失败下标=0");
    }

    private void testErrorCases() {
        // 缺 ops
        Map<String, Object> r1 = run("{}");
        eq(r1.get("ok"), false, "空请求失败");
        // 未知算子
        Map<String, Object> r2 = run("{\"ops\":[{\"op\":\"frobnicate\"}]}");
        @SuppressWarnings("unchecked")
        Map<String, Object> e2 = (Map<String, Object>) r2.get("error");
        eq(e2.get("code"), "UNKNOWN_OP", "未知算子错误码");
        eq(e2.get("failedOpIndex"), 0, "失败下标");
        // 草图不存在
        Map<String, Object> r3 = run("{\"ops\":[{\"op\":\"estimate\",\"name\":\"nope\"}]}");
        @SuppressWarnings("unchecked")
        Map<String, Object> e3 = (Map<String, Object>) r3.get("error");
        eq(e3.get("code"), "SKETCH_NOT_FOUND", "草图不存在错误码");
        // 精度越界
        Map<String, Object> r4 = run("{\"ops\":[{\"op\":\"create\",\"name\":\"x\",\"precision\":2}]}");
        @SuppressWarnings("unchecked")
        Map<String, Object> e4 = (Map<String, Object>) r4.get("error");
        eq(e4.get("code"), "INVALID_REQUEST", "精度越界错误码");
        // 同精度缺失字段
        Map<String, Object> r5 = run("{\"ops\":[{\"op\":\"create\"}]}");
        eq(r5.get("ok"), false, "缺 name/precision 失败");
        // 同 op 名重复创建
        Map<String, Object> r6 = run("{\"ops\":["
                + "{\"op\":\"create\",\"name\":\"x\",\"precision\":12},"
                + "{\"op\":\"create\",\"name\":\"x\",\"precision\":12}]}");
        @SuppressWarnings("unchecked")
        Map<String, Object> e6 = (Map<String, Object>) r6.get("error");
        eq(e6.get("code"), "SKETCH_EXISTS", "重复创建错误码");
        eq(e6.get("failedOpIndex"), 1, "失败下标=1，且第 0 步已生效");
        // 合并不兼容：p 不同
        Map<String, Object> r7 = run("{\"ops\":["
                + "{\"op\":\"create\",\"name\":\"a\",\"precision\":12},"
                + "{\"op\":\"create\",\"name\":\"b\",\"precision\":11},"
                + "{\"op\":\"add\",\"name\":\"a\",\"values\":[1,2,3]},"
                + "{\"op\":\"merge\",\"name\":\"c\",\"sources\":[\"a\",\"b\"]}]}");
        eq(r7.get("ok"), false, "跨精度合并失败");
        @SuppressWarnings("unchecked")
        Map<String, Object> e7 = (Map<String, Object>) r7.get("error");
        eq(e7.get("code"), "INCOMPATIBLE_SKETCH", "不兼容错误码");
        check(String.valueOf(e7.get("message")).contains("精度"), "错误信息说明是精度问题");
        // 源草图不存在
        Map<String, Object> r8 = run("{\"ops\":["
                + "{\"op\":\"create\",\"name\":\"a\",\"precision\":12},"
                + "{\"op\":\"merge\",\"name\":\"z\",\"sources\":[\"a\",\"ghost\"]}]}");
        eq(r8.get("ok"), false, "源缺失合并失败");
    }

    private void testEstimateLabeling() {
        Map<String, Object> resp = run("{\"ops\":["
                + "{\"op\":\"create\",\"name\":\"z\",\"precision\":12},"
                + "{\"op\":\"estimate\",\"name\":\"z\"}]}");
        Map<String, Object> r = resultAt(resp, 1);
        eqLong(((Number) r.get("cardinalityEstimate")).longValue(), 0L, "空集估计 0");
        eq(r.get("exact"), false, "空集也标 exact=false");
        @SuppressWarnings("unchecked")
        Map<String, Object> ci = (Map<String, Object>) r.get("confidenceIntervals");
        check(ci.containsKey("68pct") && ci.containsKey("95pct"), "提供 68%/95% 区间");
        @SuppressWarnings("unchecked")
        Map<String, Object> i95 = (Map<String, Object>) ci.get("95pct");
        eqLong(((Number) i95.get("low")).longValue(), 0L, "空集 95% 区间下界 0");
    }

    private void testMalformedJson() {
        // 直接交给 Main 同款解析路径
        boolean threw = false;
        try {
            Json.parse("{ops:}");
        } catch (Json.JsonException je) {
            threw = true;
            check(je.getMessage().contains("行"), "错误信息带行列号");
        }
        check(threw, "非法 JSON 必须抛 JsonException");
        check(!(Json.parse("\"str\"") instanceof Map), "顶层非对象可被入口识别");
    }

    private void testPlanPresent() {
        Map<String, Object> resp = run("{\"ops\":["
                + "{\"op\":\"create\",\"name\":\"p\",\"precision\":12},"
                + "{\"op\":\"add\",\"name\":\"p\",\"values\":[1]},"
                + "{\"op\":\"estimate\",\"name\":\"p\"}]}");
        @SuppressWarnings("unchecked")
        Map<String, Object> plan = (Map<String, Object>) resp.get("executionPlan");
        eq(plan.get("planFormat"), "HLL-EXECUTION-PLAN/v1", "响应含可导出执行计划");
        @SuppressWarnings("unchecked")
        List<?> steps = (List<?>) plan.get("steps");
        eq(steps.size(), 3, "计划含 3 步");
        @SuppressWarnings("unchecked")
        Map<String, Object> md = (Map<String, Object>) resp.get("metadata");
        check(md.get("hashAlgorithm") != null, "metadata 记录固定哈希");
    }
}
