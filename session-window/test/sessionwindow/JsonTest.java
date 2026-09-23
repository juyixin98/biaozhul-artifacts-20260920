package sessionwindow;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** Round-trip and edge-case tests for the minimal JSON helper. */
final class JsonTest {

    private JsonTest() {
    }

    static TestRunner build() {
        return new TestRunner("Json unit tests")
                .test("round-trips nested object/array", () -> {
                    Map<String, Object> root = new LinkedHashMap<>();
                    root.put("name", "k@0");
                    root.put("version", 2L);
                    root.put("ok", true);
                    root.put("missing", null);
                    root.put("ids", List.of("e0", "e10", "e20"));
                    String json = Json.write(root);
                    Map<String, Object> back = Json.parseObject(json);
                    TestRunner.eq(back.get("name"), "k@0", "string");
                    TestRunner.eq(back.get("version"), 2.0, "number (parsed as double)");
                    TestRunner.eq(back.get("ok"), Boolean.TRUE, "boolean");
                    TestRunner.isTrue(back.containsKey("missing") && back.get("missing") == null,
                            "null preserved");
                    TestRunner.eq(back.get("ids"), List.of("e0", "e10", "e20"), "array");
                })
                .test("integers serialize without decimal point", () -> {
                    String json = Json.write(Map.of("v", 42L));
                    TestRunner.isTrue(json.contains("\"v\":42"), "integer style: " + json);
                })
                .test("escapes quotes, backslashes and control chars", () -> {
                    String raw = "a\"b\\c\n\td";
                    String json = Json.write(Map.of("v", raw));
                    Map<String, Object> back = Json.parseObject(json);
                    TestRunner.eq(back.get("v"), raw, "escaped round trip");
                })
                .test("rejects malformed json", () -> {
                    boolean threw = false;
                    try {
                        Json.parse("{");
                    } catch (IllegalArgumentException e) {
                        threw = true;
                    }
                    TestRunner.isTrue(threw, "unterminated object rejected");
                })
                .test("rejects trailing characters", () -> {
                    boolean threw = false;
                    try {
                        Json.parse("{}garbage");
                    } catch (IllegalArgumentException e) {
                        threw = true;
                    }
                    TestRunner.isTrue(threw, "trailing garbage rejected");
                })
                .test("preserves insertion order on output", () -> {
                    Map<String, Object> m = new LinkedHashMap<>();
                    m.put("z", 1);
                    m.put("a", 2);
                    m.put("m", 3);
                    String json = Json.write(m);
                    int z = json.indexOf("\"z\"");
                    int a = json.indexOf("\"a\"");
                    int mm = json.indexOf("\"m\"");
                    TestRunner.isTrue(z < a && a < mm, "key order preserved");
                })
                .test("empty containers serialize and parse", () -> {
                    TestRunner.eq(Json.parse("[]"), List.of(), "empty array");
                    TestRunner.eq(Json.parseObject("{}").size(), 0, "empty object");
                });
    }
}
