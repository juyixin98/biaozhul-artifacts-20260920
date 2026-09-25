package com.timeconv;

import com.timeconv.json.Json;
import com.timeconv.json.JsonNumber;

import java.util.Map;

import static com.timeconv.TestRunner.assertEquals;
import static com.timeconv.TestRunner.assertTrue;
import static com.timeconv.TestRunner.test;

/** Tests for the JSON reader/writer, especially exact number handling. */
public final class JsonTest {

    public static void register() {
        test("integers survive parsing exactly (no double)", () -> {
            Map<String, Object> m = Json.parseObject("{\"v\": 9223372036854775807}");
            assertEquals("9223372036854775807", ((JsonNumber) m.get("v")).raw());
        });
        test("negative and huge integers survive exactly", () -> {
            Map<String, Object> m = Json.parseObject("{\"v\": -99999999999999999999999999}");
            assertEquals("-99999999999999999999999999", ((JsonNumber) m.get("v")).raw());
        });
        test("round trip write/parse of nested structure", () -> {
            String text = "{\"a\":[1,\"x\",true,null],\"b\":{\"c\":-1.5}}";
            Object v = Json.parse(text);
            assertEquals(text, Json.write(v));
        });
        test("string escapes round trip", () -> {
            String s = "quote\" backslash\\ newline\n tab\t unicodeA";
            String written = Json.write(s);
            assertEquals(s, Json.parse(written));
        });
        test("malformed JSON rejected", () -> {
            boolean thrown = false;
            try {
                Json.parse("{bad");
            } catch (com.timeconv.json.JsonParseException e) {
                thrown = true;
            }
            assertTrue(thrown, "expected JsonParseException");
        });
    }

    private JsonTest() {
    }
}
