package com.tvl.test;

import com.tvl.json.Json;
import com.tvl.json.JsonException;
import com.tvl.json.JsonWriter;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** JSON 解析/输出基础测试，保证零依赖序列化可用。 */
final class JsonTest {

    private JsonTest() {
    }

    static void register(TestRunner runner) {
        runner.add("JSON: 解析数字/字符串/null/布尔/嵌套", a -> {
            Object v = Json.parse("{\"i\":1,\"d\":2.5,\"s\":\"x\\ny\",\"n\":null,"
                    + "\"b\":true,\"arr\":[1,2,{\"k\":\"v\"}],\"neg\":-3,\"exp\":1e2}");
            Map<?, ?> m = (Map<?, ?>) v;
            a.eq(m.get("i"), 1L, "整数 -> Long");
            a.eq(m.get("d"), 2.5d, "小数 -> Double");
            a.eq(m.get("s"), "x\ny", "转义换行");
            a.check(m.containsKey("n") && m.get("n") == null, "null 保留键");
            a.eq(m.get("b"), Boolean.TRUE, "布尔");
            a.eq(((List<?>) m.get("arr")).size(), 3, "数组");
            a.eq(m.get("neg"), -3L, "负数");
            a.eq(m.get("exp"), 100.0d, "科学计数 -> Double");
        });

        runner.add("JSON: 非法输入被拒绝", a -> {
            String[] bad = {"", "   ", "{", "}", "[1,]", "{\"a\" 1}", "tru", "01", "1.", "\"x"};
            for (String s : bad) {
                a.expectThrows(JsonException.class, () -> Json.parse(s), "非法 JSON: " + s);
            }
        });

        runner.add("JSON: 输出器处理 Map/List/null/转义", a -> {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("ok", true);
            m.put("null", null);
            m.put("list", List.of(1L, "a\"b"));
            String s = JsonWriter.write(m);
            a.check(s.contains("\"ok\":true"), "布尔输出: " + s);
            a.check(s.contains("\"null\":null"), "null 输出");
            a.check(s.contains("\"a\\\"b\""), "引号转义");
            // 可逆
            Object back = Json.parse(s);
            a.eq(((Map<?, ?>) back).get("list"), List.of(1L, "a\"b"), "输出再解析一致");
        });
    }
}
