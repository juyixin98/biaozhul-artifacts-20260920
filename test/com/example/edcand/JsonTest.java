package com.example.edcand;

import java.util.List;
import java.util.Map;

/** 自写 JSON 工具的往返测试，含 Unicode 转义与增补平面字符。 */
public final class JsonTest {

    private JsonTest() {
    }

    public static void register(TestRunner r) {
        r.add("json: round-trip object with unicode and emoji", t -> {
            Map<String, Object> m = new java.util.LinkedHashMap<>();
            m.put("term", "café 😀");
            m.put("distance", 2L);
            m.put("nested", Map.of("a", 1L));
            // List 中含 null：List.of 不允许 null，用 Arrays.asList
            m.put("list", java.util.Arrays.asList("x", "日本語", true, null, 3.5));
            String s = Json.write(m);
            @SuppressWarnings("unchecked")
            Map<String, Object> back = (Map<String, Object>) Json.parse(s);
            t.eq(back.get("term"), "café 😀", "unicode survives round trip");
            t.eq(back.get("distance"), 2L, "long survives");
            t.eq(((Map<?, ?>) back.get("nested")).get("a"), 1L, "nested map");
            t.eq(((List<?>) back.get("list")).get(1), "日本語", "list unicode");
            t.eq(((List<?>) back.get("list")).get(2), Boolean.TRUE, "list bool");
        });
        r.add("json: parses \\u escape and rejects bad input", t -> {
            t.eq(Json.parse("\"caf\\u00e9\""), "café", "u escape");
            t.eq(Json.parse("42"), 42L, "int");
            t.eq(Json.parse("-1.5e2"), -150.0, "double");
            t.eq(Json.parse("[ ]"), List.of(), "empty array");
            t.eq(Json.parse("{ }"), Map.of(), "empty object");
            try {
                Json.parse("{");
                t.fail("unterminated object must throw");
            } catch (IllegalArgumentException ok) {
                // expected
            }
            try {
                Json.parse("{}x");
                t.fail("trailing chars must throw");
            } catch (IllegalArgumentException ok) {
                // expected
            }
        });
    }
}
