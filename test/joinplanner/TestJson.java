package joinplanner;

import joinplanner.json.JsonException;
import joinplanner.json.JsonParser;
import joinplanner.json.JsonWriter;

import java.util.List;
import java.util.Map;

public class TestJson {

    public static void main(String[] args) {
        TestFramework t = new TestFramework();
        test(t);
        report(t, "TestJson");
    }

    static void test(TestFramework t) {
        Object v = JsonParser.parse(
                "{\"a\": 1, \"b\": [true, false, null, \"x\\\"y\", -2.5e3], \"c\": {}}");
        Map<?, ?> m = (Map<?, ?>) v;
        t.eq(m.get("a"), 1.0, "integer parses as double");
        List<?> list = (List<?>) m.get("b");
        t.eq(list.get(0), Boolean.TRUE, "true");
        t.eq(list.get(2), null, "null");
        t.eq(list.get(3), "x\"y", "escaped string");
        t.eq(list.get(4), -2500.0, "exponent number");

        t.throwsContaining("Trailing", () -> JsonParser.parse("{}garbage"));
        t.throwsContaining("Unexpected", () -> JsonParser.parse("[1,]"));
        t.throwsContaining("Unterminated", () -> JsonParser.parse("\"abc"));

        // Round trip.
        String json = "{\"name\":\"计划\",\"rows\":1000,\"sel\":1.0E-4}";
        Object parsed = JsonParser.parse(json);
        String again = JsonWriter.write(parsed);
        @SuppressWarnings("unchecked")
        Map<String, Object> reparsed = (Map<String, Object>) JsonParser.parse(again);
        t.eq(reparsed.get("name"), "计划", "unicode round trip");
        t.eq(reparsed.get("rows"), 1000.0, "int round trip");
        t.approx((Double) reparsed.get("sel"), 1e-4, 1e-12, "small double round trip");

        // Integer-like doubles render without a trailing .0.
        t.eq(JsonWriter.formatNumber(42.0), "42", "number formatting integer");
        t.eq(JsonWriter.formatNumber(42.5), "42.5", "number formatting fraction");

        // Nested structure reachable.
        Object deep = JsonParser.parse("[[1,2],[3,[4]]]");
        t.eq(((List<?>) ((List<?>) deep).get(1)).get(0), 3.0, "nested arrays");
    }

    static void report(TestFramework t, String name) {
        if (t.failures.isEmpty()) {
            System.out.println("PASS " + name + " (" + t.checks() + " checks)");
        } else {
            System.out.println("FAIL " + name);
            t.failures.forEach(f -> System.out.println("  - " + f));
            System.exit(1);
        }
    }
}
