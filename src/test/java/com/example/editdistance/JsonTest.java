package com.example.editdistance;

import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;

class JsonTest {

    @Test
    void roundTrip() {
        Map<String, Object> value = Map.of(
                "query", "café😀",
                "k", 2L,
                "nested", Map.of("a", List.of(1L, 2.5, true, "x")));
        String text = Json.write(value);
        assertEquals(value, Json.parse(text));
    }

    @Test
    void parsesUnicodeEscapesAndSurrogatePairs() {
        assertEquals("café", Json.parse("\"caf\\u00E9\""));
        // 😀 as a UTF-16 surrogate pair escape
        assertEquals("😀", Json.parse("\"\\uD83D\\uDE00\""));
        assertEquals("a\nb\t\"c\"", Json.parse("\"a\\nb\\t\\\"c\\\"\""));
    }

    @Test
    void rejectsMalformedInput() {
        assertThrows(Json.JsonException.class, () -> Json.parse("{"));
        assertThrows(Json.JsonException.class, () -> Json.parse("{\"a\":}"));
        assertThrows(Json.JsonException.class, () -> Json.parse("[1,]"));
        assertThrows(Json.JsonException.class, () -> Json.parse("\"unterminated"));
        assertThrows(Json.JsonException.class, () -> Json.parse("true false"));
    }
}
