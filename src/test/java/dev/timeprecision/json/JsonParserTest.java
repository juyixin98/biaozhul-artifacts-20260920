package dev.timeprecision.json;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertInstanceOf;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

class JsonParserTest {

    @Test
    void parsesFlatObject() {
        JsonValue v = JsonParser.parse("{\"a\": 1, \"b\": \"x\", \"c\": true, \"d\": null}");
        JsonValue.Obj obj = assertInstanceOf(JsonValue.Obj.class, v);
        assertEquals(new JsonValue.Num("1"), obj.get("a"));
        assertEquals(new JsonValue.Str("x"), obj.get("b"));
        assertEquals(new JsonValue.Bool(true), obj.get("c"));
        assertEquals(JsonValue.Null.INSTANCE, obj.get("d"));
    }

    @Test
    void keepsNumberLiteralsRaw() {
        JsonValue.Obj obj = (JsonValue.Obj) JsonParser.parse("{\"v\": -9223372036854775808}");
        assertEquals("-9223372036854775808", ((JsonValue.Num) obj.get("v")).raw());
    }

    @Test
    void parsesNestedStructuresAndEscapes() {
        JsonValue.Obj obj = (JsonValue.Obj) JsonParser.parse(
                "{\"list\": [1, {\"s\": \"a\\nb\\u0041\"}]}");
        JsonValue.Arr arr = (JsonValue.Arr) obj.get("list");
        JsonValue.Obj inner = (JsonValue.Obj) arr.items().get(1);
        assertEquals("a\nbA", ((JsonValue.Str) inner.get("s")).value());
    }

    @Test
    void rejectsMalformedInput() {
        assertThrows(JsonParseException.class, () -> JsonParser.parse(""));
        assertThrows(JsonParseException.class, () -> JsonParser.parse("{"));
        assertThrows(JsonParseException.class, () -> JsonParser.parse("{\"a\":1} extra"));
        assertThrows(JsonParseException.class, () -> JsonParser.parse("{\"a\": 01}"));
        assertThrows(JsonParseException.class, () -> JsonParser.parse("[1,]"));
        assertThrows(JsonParseException.class, () -> JsonParser.parse(null));
    }

    @Test
    void writerRoundTrips() {
        String source = "{\"a\":[1,-2,\"x\\ty\"],\"b\":{\"c\":null},\"n\":-0.5e3}";
        JsonValue value = JsonParser.parse(source);
        JsonValue reparsed = JsonParser.parse(JsonWriter.write(value));
        assertEquals(value, reparsed);
    }

    @Test
    void writerEscapesControlCharacters() {
        String out = JsonWriter.write(new JsonValue.Str("a\"b\\c\u0001d"));
        assertTrue(out.contains("\\\""));
        assertTrue(out.contains("\\\\"));
        assertTrue(out.contains("\\u0001"));
    }
}
