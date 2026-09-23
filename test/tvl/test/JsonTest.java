package tvl.test;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import tvl.json.Json;
import tvl.json.JsonException;

/** 自研 JSON 模块的往返、转义、数字类型与非法输入测试。 */
public class JsonTest extends TestBase {

    @Override
    public String name() {
        return "JSON parser/writer";
    }

    @Override
    public void run() {
        roundTrips();
        types();
        escapes();
        malformed();
    }

    private void roundTrips() {
        Map<String, Object> obj = new LinkedHashMap<>();
        obj.put("name", "ada");
        obj.put("age", 36L);
        obj.put("active", true);
        obj.put("nick", null);
        obj.put("tags", List.of("a", "b", "c"));
        String json = Json.write(obj);
        Object parsed = Json.parse(json);
        expectEq(obj, parsed, "object round-trip with nested list");
        expectEq(List.of(1L, 2L, 3L), Json.parse("[1,2,3]"), "array round-trip");
        // 键顺序保留
        @SuppressWarnings("unchecked")
        List<String> keys = new java.util.ArrayList<>(((Map<String, Object>) parsed).keySet());
        expectEq(List.of("name", "age", "active", "nick", "tags"), keys, "key order preserved");
    }

    private void types() {
        check(Json.parse("42") instanceof Long, "integer parses as Long");
        check(Json.parse("3.5") instanceof Double, "decimal parses as Double");
        check(Json.parse("true") instanceof Boolean b && b, "true boolean");
        check(Json.parse("null") == null, "null literal");
        check(Json.parse("\"x\"") instanceof String, "string type");
    }

    private void escapes() {
        expectEq("a\"b\\c\n\tt",
                Json.parse("\"a\\\"b\\\\c\\n\\tt\""), "escape sequences");
        expectEq("✓", Json.parse("\"\\u2713\""), "\\u escape");
        expectEq("it's", Json.parse("\"it's\""), "raw apostrophe in JSON string");
        // 书写器转义
        String written = Json.write("a\nb");
        expectEq("\"a\\nb\"", written, "newline escaped on write");
    }

    private void malformed() {
        expectJsonError("{", "unclosed object");
        expectJsonError("[1,]", "trailing comma array");
        expectJsonError("{\"a\"}", "object missing value");
        expectJsonError("tru", "truncated literal");
        expectJsonError("01", "leading zero");
        expectJsonError("\"unterminated", "unterminated string");
        expectJsonError("{}x", "trailing chars");
    }

    private void expectJsonError(String text, String label) {
        try {
            Json.parse(text);
            fail(label + " — expected JsonException for <" + text + ">");
        } catch (JsonException ex) {
            check(true, label + " at offset " + ex.position);
        }
    }
}
