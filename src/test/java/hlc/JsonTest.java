package hlc;

import java.util.List;
import java.util.Map;

public class JsonTest {

    @Test("nested objects, arrays and unicode round-trip")
    void roundTrip() {
        // ArrayList (not List.of) so null is permitted as a JSON value.
        java.util.List<Object> flags = new java.util.ArrayList<>();
        flags.add(true);
        flags.add(false);
        flags.add(null);
        Map<String, Object> doc = Map.of(
                "name", "alice",
                "micros", 1_700_000_000_000_000L,
                "flags", flags,
                "nested", Map.of("hlc", "1000:7", "zone", "Asia/Shanghai 上海"));
        String json = Json.write(doc);
        Map<String, Object> parsed = Json.parseObject(json);
        TestRunner.assertEquals(doc, parsed, "parse(write) is identity");
        TestRunner.assertTrue(Json.writePretty(doc).contains("\n"), "pretty printer uses newlines");
    }

    @Test("string escapes are decoded")
    void escapes() {
        Map<String, Object> p = Json.parseObject("{\"a\":\"line1\\nline2\\t\\u0041\"}");
        TestRunner.assertEquals("line1\nline2\tA", p.get("a"), "escapes decoded");
    }

    @Test("malformed JSON is rejected")
    void malformed() {
        String[] bad = {"", "not json", "{", "[1,]", "{\"a\":}", "{'a':1}", "123x", "[1 2]"};
        for (String s : bad) {
            TestRunner.assertThrows(HLCException.class, () -> Json.parse(s));
        }
        TestRunner.assertThrows(HLCException.class, () -> Json.parseObject("[1,2]"));
    }

    @Test("required field helpers fail fast")
    void fieldHelpers() {
        Map<String, Object> ok = Map.of("s", "x", "n", 5L);
        TestRunner.assertEquals("x", Json.requireString(ok, "s"), "string");
        TestRunner.assertEquals(5L, Json.requireLong(ok, "n"), "long");
        TestRunner.assertThrows(HLCException.class, () -> Json.requireString(ok, "n"));
        TestRunner.assertThrows(HLCException.class, () -> Json.requireLong(ok, "missing"));
    }
}
