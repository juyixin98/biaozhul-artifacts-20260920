package com.opp16.engine.tests;

import com.opp16.engine.json.Json;

public final class JsonTests {

    private JsonTests() {}

    public static void run() {
        Assert.suite("Json");

        Json.Value v = Json.parse("{\"a\": 1, \"b\": [true, false, null, \"x\"], \"c\": -3.5}");
        Assert.check("root is object", v.isObject());
        Assert.eq("integer field", v.asObject().get("a").asLong(), 1L);
        Json.Arr arr = v.asObject().get("b").asArray();
        Assert.eq("array size", arr.size(), 4);
        Assert.check("bool true", arr.get(0).asBoolean());
        Assert.check("bool false", !arr.get(1).asBoolean());
        Assert.check("null", arr.get(2).isNull());
        Assert.eq("string", arr.get(3).asString(), "x");
        Assert.eq("double", v.asObject().get("c").asDouble(), -3.5);

        // escapes and unicode:
        // JSON source "a\nb\t\u0041c" decodes the \u0041 escape to 'A'
        Json.Value u = Json.parse("\"a\\nb\\t\\u0041c\"");
        Assert.eq("escapes decoded", u.asString(), "a\nb\tAc");
        // JSON source with an escaped backslash keeps literal text "\u0041" (not decoded twice)
        Json.Value literal = Json.parse("\"\\\\u0041\"");
        Assert.eq("escaped backslash is literal", literal.asString(), "\\u0041");
        Json.Value unicode = Json.parse("\"\\u0041\"");
        Assert.eq("unicode escape decoded to A", unicode.asString(), "A");

        // numbers: long vs decimal kept distinct
        Assert.check("int token is non-decimal", !((Json.Num) Json.parse("42")).decimal);
        Assert.check("float token is decimal", ((Json.Num) Json.parse("42.0")).decimal);

        // round trip preserves structure
        String text = "{\"k\":[1,2,{\"x\":null}],\"s\":\"v\",\"t\":true}";
        Json.Value once = Json.parse(text);
        Json.Value twice = Json.parse(once.render());
        Assert.check("compact round trip equal", once.equals(twice));

        // pretty render is parseable and equal
        Json.Value pretty = Json.parse(once.render(true));
        Assert.check("pretty round trip equal", once.equals(pretty));

        Assert.throwsWith("trailing chars rejected", Json.JsonException.class, "trailing",
                () -> Json.parse("1 2"));
        Assert.throwsWith("unterminated string rejected", Json.JsonException.class, "unterminated",
                () -> Json.parse("\"abc"));
        Assert.throwsWith("bad literal rejected", Json.JsonException.class, "invalid literal",
                () -> Json.parse("tru"));
        Assert.throwsWith("missing key rejected", Json.JsonException.class, "missing key",
                () -> new Json.Obj().get("nope"));
    }
}
