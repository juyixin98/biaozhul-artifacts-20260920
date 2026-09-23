package phj.json;

import phj.Test;
import phj.TestRunner;

import java.util.List;
import java.util.Map;

public class JsonTest {

    @Test
    public void parsePrimitives() {
        TestRunner.assertEquals(42L, ((Number) Json.parse("42")).longValue());
        TestRunner.assertEquals(3.5, ((Number) Json.parse("3.5")).doubleValue(), 0.0);
        TestRunner.assertEquals("hi", Json.parse("\"hi\""));
        TestRunner.assertEquals(Boolean.TRUE, Json.parse("true"));
        TestRunner.assertEquals(null, Json.parse("null"));
    }

    @Test
    public void parseNested() {
        Object v = Json.parse("{\"a\":[1,2,{\"b\":null}],\"c\":\"x\\n\"}");
        Map<String, Object> m = Json.asObj(v, "root");
        List<Object> arr = Json.asArr(m.get("a"), "a");
        TestRunner.assertEquals(1L, ((Number) arr.get(0)).longValue());
        TestRunner.assertEquals(null, Json.asObj(arr.get(2), "a2").get("b"));
        TestRunner.assertEquals("x\n", m.get("c"));
    }

    @Test
    public void parseUnicodeAndEscapes() {
        TestRunner.assertEquals("A中", Json.parse("\"A\\u4e2d\""));
        TestRunner.assertEquals("a\\b\"c", Json.parse("\"a\\\\b\\\"c\""));
    }

    @Test
    public void parseErrors() {
        expectFail("{");
        expectFail("[1,]");
        expectFail("\"unterminated");
        expectFail("123abc");
        expectFail("{\"a\":1}extra");
    }

    private void expectFail(String s) {
        try {
            Json.parse(s);
            TestRunner.fail("应当解析失败：" + s);
        } catch (JsonException expected) {
            // ok
        }
    }

    @Test
    public void roundTripStable() {
        Map<String, Object> m = new java.util.LinkedHashMap<>();
        m.put("n", 1L);
        m.put("d", 2.5);
        m.put("s", "中文\tok");
        m.put("b", true);
        m.put("z", null);
        m.put("a", List.of(1L, 2L, 3L));
        String json = Json.write(m);
        Object back = Json.parse(json);
        TestRunner.assertTrue(back.equals(m), "round-trip 应还原：" + json);
    }

    @Test
    public void emptyCollections() {
        TestRunner.assertTrue(((Map<?, ?>) Json.parse("{}")).isEmpty());
        TestRunner.assertTrue(((List<?>) Json.parse("[]")).isEmpty());
        TestRunner.assertEquals("{}", Json.write(Map.of()));
        TestRunner.assertEquals("[]", Json.write(List.of()));
    }
}
