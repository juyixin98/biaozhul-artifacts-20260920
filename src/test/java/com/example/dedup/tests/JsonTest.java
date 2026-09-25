package com.example.dedup.tests;

import com.example.dedup.json.Json;

/** Round-trip and canonicalization tests for the small JSON module. */
public final class JsonTest {

    public static void register(TestRunner r) {
        r.run("json: round-trip nested object/array/string/number/bool/null", () -> {
            String src = "{\"a\":[1,2.5,true,false,null,\"s\"],\"b\":{\"c\":-7}}";
            Json.Value v = Json.parse(src);
            String out = Json.write(v);
            Json.Value reparsed = Json.parse(out);
            TestRunner.assertEquals(Json.canonical(v), Json.canonical(reparsed), "round trip canonical equal");
        });

        r.run("json: unicode/escapes round trip", () -> {
            String src = "{\"s\":\"line\\nbreak\\ttab\\\\ \\\"q\\\" 漢字\"}";
            Json.Value v = Json.parse(src);
            String s = ((Json.JsonString) ((Json.JsonObject) v).get("s")).value();
            TestRunner.assertTrue(s.contains("漢字"), "unicode literal preserved");
            TestRunner.assertTrue(s.contains("\n"), "escaped newline decoded");
        });

        r.run("json: canonical sorts object keys", () -> {
            Json.JsonObject o = Json.obj();
            o.members().put("z", Json.num(1));
            o.members().put("a", Json.num(2));
            TestRunner.assertEquals("{\"a\":2,\"z\":1}", Json.canonical(o), "sorted keys");
        });

        r.run("json: malformed input rejected", () -> {
            for (String bad : new String[]{"{", "[1,]", "{\"a\":}", "tru", "01", "\"unterminated"}) {
                boolean threw = false;
                try {
                    Json.parse(bad);
                } catch (Json.JsonException e) {
                    threw = true;
                }
                TestRunner.assertTrue(threw, "should reject: " + bad);
            }
        });

        r.run("json: large numbers preserve as long", () -> {
            Json.Value v = Json.parse("{\"n\":9000000000000000000}");
            long n = ((Json.JsonObject) v).getLong("n", 0);
            TestRunner.assertEquals(9_000_000_000_000_000_000L, n, "long precision");
        });
    }
}
