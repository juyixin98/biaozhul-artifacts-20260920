package com.example.phrasesearch.tests;

import com.example.phrasesearch.json.Json;
import com.example.phrasesearch.json.JsonException;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** JSON 解析/序列化往返与错误处理测试。 */
final class JsonTest {

    private JsonTest() {
    }

    static void run() {
        roundTrip();
        parseNested();
        escapes();
        numbers();
        errors();
        unicode();
    }

    private static void roundTrip() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("query", "quick brown fox");
        m.put("slop", 1L);
        m.put("crossField", true);
        m.put("missing", null);
        m.put("docs", List.of("doc1", "doc2"));
        String json = Json.write(m);
        Object back = Json.parse(json);
        Assert.equals("JSON 往返一致", m, back);
    }

    private static void parseNested() {
        Object v = Json.parse("{\"a\":{\"b\":[1,2,{\"c\":true}]}}");
        Map<?, ?> root = (Map<?, ?>) v;
        Map<?, ?> a = (Map<?, ?>) root.get("a");
        List<?> b = (List<?>) a.get("b");
        Assert.equals("嵌套数组取值", 2L, b.get(1));
        Assert.equals("嵌套对象取值", Boolean.TRUE,
                ((Map<?, ?>) b.get(2)).get("c"));
    }

    private static void escapes() {
        String s = "a\"b\\c\n\td";
        String json = Json.write(Map.of("k", s));
        Map<?, ?> back = Json.parseObject(json);
        Assert.equals("转义往返", s, back.get("k"));
    }

    private static void numbers() {
        Map<?, ?> m = Json.parseObject("{\"i\":42,\"neg\":-7,\"d\":3.5,\"exp\":1e2}");
        Assert.equals("整数解析", 42L, m.get("i"));
        Assert.equals("负整数", -7L, m.get("neg"));
        Assert.equals("小数", 3.5d, m.get("d"));
        Assert.equals("科学计数", 100d, m.get("exp"));
    }

    private static void errors() {
        Assert.check("空输入报错", () -> {
            try { Json.parse("  "); return false; } catch (JsonException e) { return true; }
        });
        Assert.check("尾随字符报错", () -> {
            try { Json.parse("{}x"); return false; } catch (JsonException e) { return true; }
        });
        Assert.check("缺逗号报错", () -> {
            try { Json.parse("{\"a\":1 \"b\":2}"); return false; }
            catch (JsonException e) { return true; }
        });
        Assert.check("未闭合字符串报错", () -> {
            try { Json.parse("\"abc"); return false; } catch (JsonException e) { return true; }
        });
    }

    private static void unicode() {
        Map<?, ?> m = Json.parseObject("{\"k\":\"caf\\u00e9\"}");
        Assert.equals("\\uXXXX 转义", "café", m.get("k"));
    }
}
