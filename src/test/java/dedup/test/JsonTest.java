package dedup.test;

import dedup.Json;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static dedup.test.Assert.assertEquals;
import static dedup.test.Assert.assertThrows;
import static dedup.test.Assert.assertTrue;

public class JsonTest {

    @Test
    public void parsesNestedStructure() {
        Map<String, Object> v = Json.parseObject(
                "{\"a\":1,\"b\":[true,false,null,\"s\"],\"c\":{\"d\":-7}}");
        assertEquals(1L, ((Number) v.get("a")).longValue(), "a");
        List<?> list = (List<?>) v.get("b");
        assertEquals(Boolean.TRUE, list.get(0), "b0");
        assertEquals(null, list.get(2), "b2");
        assertEquals("s", list.get(3), "b3");
        assertEquals(-7L, ((Number) ((Map<?, ?>) v.get("c")).get("d")).longValue(), "d");
    }

    @Test
    public void distinguishesLongAndDouble() {
        Map<String, Object> v = Json.parseObject("{\"i\":42,\"f\":42.5,\"e\":1e3}");
        assertTrue(v.get("i") instanceof Long, "integral should be Long");
        assertTrue(v.get("f") instanceof Double, "fractional should be Double");
        assertTrue(v.get("e") instanceof Double, "exponent should be Double");
    }

    @Test
    public void roundTripsAndEscapes() {
        Map<String, Object> v = new LinkedHashMap<>();
        v.put("line", "a\nb\t\"q\"\\");
        v.put("u", "é");
        String json = Json.write(v);
        Map<String, Object> back = Json.parseObject(json);
        assertEquals("a\nb\t\"q\"\\", back.get("line"), "escapes round-trip");
        assertEquals("é", back.get("u"), "unicode round-trip");
    }

    @Test
    public void rejectsTrailingGarbageAndBadInput() {
        assertThrows(Json.JsonException.class, () -> Json.parse("{}x"));
        assertThrows(Json.JsonException.class, () -> Json.parse("{\"a\":}"));
        assertThrows(Json.JsonException.class, () -> Json.parse("[1,2,]"));
        assertThrows(Json.JsonException.class, () -> Json.parse("tru"));
    }

    @Test
    public void longAccessorsValidateFractions() {
        Map<String, Object> good = Json.parseObject("{\"n\":3}");
        assertEquals(3L, Json.reqLong(good, "n"), "int value accepted");
        Map<String, Object> bad = Json.parseObject("{\"n\":3.5}");
        assertThrows(Json.JsonException.class, () -> Json.reqLong(bad, "n"));
        assertThrows(Json.JsonException.class,
                () -> Json.reqString(Json.parseObject("{}"), "missing"));
    }
}
