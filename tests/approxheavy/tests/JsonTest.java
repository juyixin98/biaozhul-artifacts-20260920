package approxheavy.tests;

import approxheavy.json.Json;

import java.util.List;
import java.util.Map;

/** Round trips and edge cases for the hand-rolled JSON parser/writer. */
public final class JsonTest {
    public static void main(String[] args) {
        TestRunner runner = new TestRunner("JsonTest");

        runner.add("parses nested object/array with escapes", () -> {
            Map<String, Object> m = Json.parseObject(
                    "{\"a\":\"v\\n\\\"x\",\"b\":[1,2.5,true,false,null],\"c\":{\"d\":-7}}");
            TestRunner.check("v\n\"x".equals(m.get("a")), "escape decode");
            List<Object> b = Json.list(m.get("b"));
            TestRunner.checkEq(((Number) b.get(0)).longValue(), 1L, "int");
            TestRunner.check(((Number) b.get(1)).doubleValue() == 2.5, "double");
            TestRunner.check(Boolean.TRUE.equals(b.get(2)), "true");
            TestRunner.check(Boolean.FALSE.equals(b.get(3)), "false");
            TestRunner.check(b.get(4) == null, "null");
            TestRunner.checkEq(((Number) Json.object(m.get("c")).get("d")).longValue(), -7L, "neg");
        });

        runner.add("write -> parse round trip preserves data", () -> {
            Map<String, Object> m = Map.of("k1", "v1", "k2", 42L, "k3", List.of(1L, 2L, 3L));
            Object reparsed = Json.parse(Json.write(m));
            TestRunner.check(m.equals(reparsed), "round trip equal: " + Json.write(m));
        });

        runner.add("rejects malformed input", () -> {
            expectBad("{");
            expectBad("{\"a\"}");
            expectBad("[1,]");
            expectBad("tru");
            expectBad("123abc");
        });

        runner.add("rejects trailing characters", () -> expectBad("{} x"));

        runner.add("unicode escape decoded", () -> {
            Map<String, Object> m = Json.parseObject("{\"s\":\"\\u0041\\u0042\"}");
            TestRunner.check("AB".equals(m.get("s")), "AB decoded");
        });

        runner.run();
    }

    private static void expectBad(String text) {
        try {
            Json.parse(text);
            throw new AssertionError("should have failed: " + text);
        } catch (RuntimeException expected) {
            // intended
        }
    }
}
