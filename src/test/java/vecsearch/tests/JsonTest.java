package vecsearch.tests;

import vecsearch.json.Json;
import vecsearch.json.JsonWriter;
import vecsearch.testutil.Assert;
import vecsearch.testutil.TestRunner;
import vecsearch.util.ApiException;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** JSON 解析/序列化往返与错误输入。 */
public final class JsonTest {

    public static void register(TestRunner r) {
        r.test("parse nested object/array/number/string/bool/null", () -> {
            Map<String, Object> m = Json.parseObject(
                    "{\"a\":1,\"b\":[1,2.5,-3],\"c\":{\"d\":\"x\"},\"e\":true,\"f\":null}");
            Assert.eq(((Number) m.get("a")).intValue(), 1);
            Assert.eq(((List<?>) m.get("b")).size(), 3);
            Assert.eq(((Number) ((List<?>) m.get("b")).get(1)).doubleValue(), 2.5);
            Assert.eq(((Map<?, ?>) m.get("c")).get("d"), "x");
            Assert.eq(m.get("e"), Boolean.TRUE);
            Assert.isTrue(m.containsKey("f") && m.get("f") == null);
        });

        r.test("order is preserved (LinkedHashMap)", () -> {
            String text = "{\"z\":1,\"a\":2,\"m\":3}";
            Assert.eq(List.copyOf(Json.parseObject(text).keySet()), List.of("z", "a", "m"));
        });

        r.test("escapes round-trip", () -> {
            String raw = "héllo\n\t\"引号\\";
            String json = JsonWriter.write(Map.of("k", raw));
            Map<String, Object> back = Json.parseObject(json);
            Assert.eq(back.get("k"), raw);
        });

        r.test("malformed JSON gives 400 ApiException", () -> {
            Assert.throws_(ApiException.class, () -> Json.parse("{oops}"), "bad object");
            Assert.throws_(ApiException.class, () -> Json.parse("[1,2,]"), "bad array");
            Assert.throws_(ApiException.class, () -> Json.parse(""), "empty");
            Assert.throws_(ApiException.class, () -> Json.parse("   "), "blank");
            Assert.throws_(ApiException.class, () -> Json.parse("123 456"), "trailing");
        });

        r.test("non-object top level rejected by parseObject", () -> {
            Assert.throws_(ApiException.class, () -> Json.parseObject("[1,2]"));
        });

        r.test("writer round trip of nested map/list", () -> {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("metric", "L2");
            m.put("count", 3);
            m.put("hits", List.of(Map.of("id", "a", "d", 0.5f), Map.of("id", "b")));
            String json = JsonWriter.write(m);
            Object back = Json.parse(json);
            Assert.eq(((Map<?, ?>) back).get("metric"), "L2");
            Assert.eq(((Number) ((Map<?, ?>) back).get("count")).intValue(), 3);
            Assert.eq(((List<?>) ((Map<?, ?>) back).get("hits")).size(), 2);
        });
    }

    private JsonTest() {
    }
}
