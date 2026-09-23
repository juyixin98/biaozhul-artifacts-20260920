package hllengine.test;

import hllengine.json.Json;
import hllengine.json.JsonException;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

public final class JsonTest implements TestRunner.Suite {

    @Override
    public void register(TestRunner.Registry r) {
        r.add("json.roundTripsScalarsAndContainers", this::roundTrip);
        r.add("json.strictParserRejectsBadFormats", this::badFormats);
        r.add("json.numbersKeepLongVsDouble", this::numberTypes);
        r.add("json.unicodeAndEscapes", this::unicode);
        r.add("json.writerEscapesControlChars", this::writerEscapes);
    }

    private void roundTrip(TestRunner.Assert a) {
        Map<String, Object> obj = new LinkedHashMap<>();
        obj.put("s", "he\tllo");
        obj.put("n", 42L);
        obj.put("d", 1.5);
        obj.put("b", true);
        obj.put("z", null);
        obj.put("arr", List.of(1L, 2L, 3L));
        String json = Json.write(obj);
        Object back = Json.parse(json);
        a.eq(back, obj, "round trip");
        a.check(Json.writePretty(obj).contains("\n"), "pretty output has newlines");
    }

    private void badFormats(TestRunner.Assert a) {
        String[] bad = {
                "", "   ", "{,}", "{\"a\" 1}", "{\"a\":1,}", "[1,2,]",
                "{'a':1}", "{a:1}", "[1 2]", "01", "-", "1.", "1e",
                "{\"a\":1,\"a\":2}", "tru", "nil", "NaN",
                "\"unterminated", "\"bad \\x escape\"", "\"ctrl\u0001\"",
                "{\"a\":} \"extra\"", "123abc",
        };
        for (String s : bad) {
            boolean threw = false;
            try {
                Json.parse(s);
            } catch (JsonException je) {
                threw = true;
            }
            a.check(threw, "parser should reject: " + quote(s));
        }
    }

    private void numberTypes(TestRunner.Assert a) {
        a.check(Json.parse("42") instanceof Long, "42 is Long");
        a.check(Json.parse("-7") instanceof Long, "-7 is Long");
        a.check(Json.parse("42.0") instanceof Double, "42.0 is Double");
        a.check(Json.parse("1e3") instanceof Double, "1e3 is Double");
        a.eq(Json.parse("1e3"), 1000.0, "1e3 value");
        a.check(Json.parse("999999999999999999") instanceof Long, "big int stays Long if it fits");
    }

    private void unicode(TestRunner.Assert a) {
        Object v = Json.parse("\"\\u0041\\u00e9\\u2603\"");
        a.eq(v, "Aé☃", "unicode escapes decode");
        a.eq(Json.parse(Json.write("中文")), "中文", "CJK round trip");
    }

    private void writerEscapes(TestRunner.Assert a) {
        String encoded = Json.write("a\"b\\c\n\t");
        a.eq(encoded, "\"a\\\"b\\\\c\\n\\t\"", "escapes exact");
    }

    private String quote(String s) {
        return s.length() > 20 ? s.substring(0, 20) + "..." : s;
    }
}
