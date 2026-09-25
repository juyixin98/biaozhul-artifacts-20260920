package tests;

import streammatch.json.Json;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** JSON 解析/序列化的往返与畸形输入测试。 */
public class JsonTest extends TestCase {

    @Override
    protected void run() {
        roundtrip();
        parsesTypes();
        escapes();
        rejectsBadJson();
        nestedStructure();
        unicode();
    }

    private void roundtrip() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("a", 1L);
        m.put("b", "hello");
        m.put("c", true);
        m.put("d", null);
        m.put("e", List.of(1L, 2L, 3L));
        String json = Json.write(m);
        Object back = Json.parse(json);
        eq(back, m, "JSON: 往返一致");
    }

    private void parsesTypes() {
        eq(Json.parse("42"), 42L, "JSON: 整数 -> Long");
        eq(Json.parse("-7"), -7L, "JSON: 负整数");
        eq(Json.parse("3.5"), 3.5, "JSON: 小数 -> Double");
        eq(Json.parse("true"), Boolean.TRUE, "JSON: true");
        eq(Json.parse("false"), Boolean.FALSE, "JSON: false");
        eq(Json.parse("null"), null, "JSON: null");
        eq(Json.parse("  [ 1 , 2 ] "), List.of(1L, 2L), "JSON: 忽略空白");
    }

    private void escapes() {
        String raw = "a\"b\\c\n\td";
        String json = Json.write(Map.of("k", raw));
        Object back = Json.parse(json);
        eq(((Map<?, ?>) back).get("k"), raw, "JSON: 转义往返");
    }

    private void rejectsBadJson() {
        String[] bad = {"", "{", "}", "[1,]", "{\"a\":}", "tru", "\"unterminated",
                "{\"a\":1,}", "01", "[1 2]", "{'a':1}"};
        for (String b : bad) {
            boolean threw = false;
            try {
                Json.parse(b);
            } catch (RuntimeException ex) {
                threw = true;
            }
            check(threw, "JSON: 畸形输入应报错: '" + b + "'");
        }
    }

    private void nestedStructure() {
        Object parsed = Json.parse("{\"matches\":[{\"aId\":\"A1\"}],\"n\":{\"x\":[1,{}]}}");
        Map<?, ?> root = (Map<?, ?>) parsed;
        List<?> matches = (List<?>) root.get("matches");
        eq(((Map<?, ?>) matches.get(0)).get("aId"), "A1", "JSON: 嵌套对象取值");
    }

    private void unicode() {
        Object v = Json.parse("\"\\u0041\\u0042\"");
        eq(v, "AB", "JSON: \\u 转义");
        String out = Json.write("中文");
        eq(Json.parse(out), "中文", "JSON: 非 ASCII 原样输出并往返");
    }
}
