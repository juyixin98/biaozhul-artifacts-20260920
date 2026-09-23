package com.example.watermark.test;

import static com.example.watermark.test.Assert.assertEquals;
import static com.example.watermark.test.Assert.assertNull;
import static com.example.watermark.test.Assert.assertThrows;
import static com.example.watermark.test.Assert.assertTrue;

import java.util.List;
import java.util.Map;

import com.example.watermark.json.Json;
import com.example.watermark.test.TestRunner.Test;

/** Round-trip and edge checks for the dependency-free JSON codec. */
public class JsonTest {

    @Test
    public void parsesAllValueTypes() {
        Map<String, Object> m = Json.parseObject(
                "{\"s\":\"a\\\\b\\n\",\"n\":42,\"f\":1.5,\"b\":false,\"nil\":null,\"arr\":[1,-2,3e2]}");
        assertEquals("a\\b\n", m.get("s"), "escapes");
        assertEquals(42L, m.get("n"), "integer -> Long");
        assertEquals(1.5, (Double) m.get("f"), 1e-9, "double");
        assertEquals(Boolean.FALSE, m.get("b"));
        assertNull(m.get("nil"), "null");
        @SuppressWarnings("unchecked")
        List<Object> arr = (List<Object>) m.get("arr");
        assertEquals(List.of(1L, -2L, 300.0), arr, "scientific notation");
    }

    @Test
    public void roundTripsNestedStructure() {
        Map<String, Object> original = Map.of(
                "config", Map.of("idleTimeoutMillis", 300L, "parts", List.of("a", "b")),
                "ok", true);
        String json = Json.write(original);
        Object reparsed = Json.parse(json);
        assertEquals(original, reparsed, "round-trip preserves data");
    }

    @Test
    public void prettyPrinterIsValidJson() {
        String pretty = Json.writePretty(Map.of("a", List.of(1L, 2L)));
        assertTrue(pretty.contains("\n"), "pretty output is multi-line");
        Object reparsed = Json.parse(pretty);
        assertEquals(Map.of("a", List.of(1L, 2L)), reparsed);
    }

    @Test
    public void rejectsMalformedInput() {
        assertThrows(IllegalArgumentException.class, "expected",
                () -> Json.parse("{\"a\":"), "unterminated object");
        assertThrows(IllegalArgumentException.class, "trailing",
                () -> Json.parse("{}garbage"), "trailing chars");
        assertThrows(IllegalArgumentException.class, "JSON object",
                () -> Json.parseObject("[1,2]"), "array where object expected");
    }
}
