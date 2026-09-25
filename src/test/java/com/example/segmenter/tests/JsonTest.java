package com.example.segmenter.tests;

import com.example.segmenter.json.Json;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static com.example.segmenter.tests.TestFramework.check;
import static com.example.segmenter.tests.TestFramework.assertEquals;
import static com.example.segmenter.tests.TestFramework.suite;

/** 最小 JSON 解析/序列化往返测试。 */
public final class JsonTest {

    private JsonTest() {
    }

    public static void run() {
        suite("json round trip");

        Map<String, Object> req = new LinkedHashMap<>();
        req.put("text", "研究生命，X");
        req.put("n", 3L);
        req.put("version", "dict_v1");
        String json = Json.write(req);
        @SuppressWarnings("unchecked")
        Map<String, Object> parsed = (Map<String, Object>) Json.parse(json);

        assertEquals("中文字符串往返", "研究生命，X", parsed.get("text"));
        assertEquals("整数往返为 Long", 3L, parsed.get("n"));
        assertEquals("版本字段", "dict_v1", parsed.get("version"));

        // 数字类型
        assertEquals("整数解析为 Long", 42L, Json.parse("42"));
        assertEquals("负数解析为 Long", -7L, Json.parse("-7"));
        assertEquals("小数解析为 Double", 1.5, Json.parse("1.5"));
        assertEquals("科学计数法", 1000.0, Json.parse("1e3"));

        // 数组、null、布尔
        assertEquals("数组", List.of(1L, 2L, 3L), Json.parse("[1,2,3]"));
        assertEquals("null", null, Json.parse("null"));
        assertEquals("true", Boolean.TRUE, Json.parse("true"));

        // 转义
        @SuppressWarnings("unchecked")
        Map<String, Object> esc = (Map<String, Object>) Json.parse("{\"a\":\"x\\ny\\t\\u4e2d\"}");
        assertEquals("转义与 unicode", "x\ny\t中", esc.get("a"));

        // 往返含未知代价等浮点字段
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("cost", 5.123456789);
        String respJson = Json.write(resp);
        @SuppressWarnings("unchecked")
        Map<String, Object> back = (Map<String, Object>) Json.parse(respJson);
        check("浮点往返误差 < 1e-15",
                Math.abs((Double) back.get("cost") - 5.123456789) < 1e-15);

        // 非法 JSON 被拒绝
        check("残缺 JSON 抛异常", parseFails("{\"a\":"));
        check("尾部多余字符抛异常", parseFails("{}x"));
        check("未闭合字符串抛异常", parseFails("\"abc"));
        check("裸控制字符抛异常", parseFails("\"ab\ncd\""));
        check("非有限数拒绝序列化", serializeFails(Double.NaN));

        // pretty 输出包含换行与缩进
        String pretty = Json.writePretty(req);
        check("pretty 输出有换行", pretty.contains("\n"));
        check("pretty 输出可再解析", Json.parse(pretty) instanceof Map);
    }

    private static boolean parseFails(String s) {
        try {
            Json.parse(s);
            return false;
        } catch (Json.JsonException e) {
            return true;
        }
    }

    private static boolean serializeFails(double d) {
        try {
            Json.write(d);
            return false;
        } catch (Json.JsonException e) {
            return true;
        }
    }
}
