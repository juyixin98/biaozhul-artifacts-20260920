package test;

import engine.json.Json;
import engine.json.JsonException;
import engine.json.JsonParser;
import engine.json.JsonWriter;
import testutil.TestHarness;

/** JSON 解析器/序列化器测试。 */
public final class JsonTest {

    public static boolean main(String[] args) {
        TestHarness t = new TestHarness("JSON 层");

        // 基本值
        t.check(JsonParser.parse("null") instanceof Json.JNull, "null 解析");
        t.eq(true, ((Json.JBool) JsonParser.parse("true")).value(), "true 解析");
        t.eq(42L, ((Json.JLong) JsonParser.parse("42")).value(), "正整数解析");
        t.eq(-7L, ((Json.JLong) JsonParser.parse("-7")).value(), "负整数解析");
        t.eq(1.5, ((Json.JDouble) JsonParser.parse("1.5")).value(), "浮点解析");
        t.eq("abc", ((Json.JStr) JsonParser.parse("\"abc\"")).value(), "字符串解析");

        // 大整数超出 long 时退化为 double
        t.check(JsonParser.parse("99999999999999999999999") instanceof Json.JDouble,
                "超 long 整数退化为 double");

        // 空白
        t.eq(1L, ((Json.JLong) JsonParser.parse("  \n\t1 \r\n")).value(), "容忍空白");

        // 数组与对象
        Json.JArr arr = (Json.JArr) JsonParser.parse("[1, 2, 3]");
        t.eq(3, arr.items.size(), "数组长度");
        t.eq(2L, ((Json.JLong) arr.items.get(1)).value(), "数组元素");

        Json.JObj obj = (Json.JObj) JsonParser.parse("{\"a\": 1, \"b\": [true, null]}");
        t.eq(1L, ((Json.JLong) obj.get("a")).value(), "对象成员 a");
        t.eq(2, ((Json.JArr) obj.get("b")).items.size(), "对象成员 b 是长度 2 数组");

        // 转义与 unicode
        Json.JStr esc = (Json.JStr) JsonParser.parse("\"a\\\\b\\n\\t\\u0041\"");
        t.eq("a\\b\n\tA", esc.value(), "转义与 \\u0041");
        Json.JStr emoji = (Json.JStr) JsonParser.parse("\"😀\"");
        t.eq("😀", emoji.value(), "非 BMP 字符直接透传");
        Json.JStr pair = (Json.JStr) JsonParser.parse("\"\\uD83D\\uDE00\"");
        t.eq("😀", pair.value(), "代理对转义");

        // 往返
        String doc = "{\"k\":[1,-2,null,\"x\"],\"y\":true}";
        String back = JsonWriter.write(JsonParser.parse(doc), false);
        t.eq(doc, back, "紧凑往返一致");

        // 缩进输出可被再次解析
        String pretty = JsonWriter.write(JsonParser.parse(doc), true);
        t.check(pretty.contains("\n"), "缩进输出含换行");
        t.eq(doc, JsonWriter.write(JsonParser.parse(pretty), false), "缩进输出重新解析后等价");

        // 中文不转义
        t.eq("\"中文\"", JsonWriter.write(new Json.JStr("中文"), false), "中文直接输出");

        // 错误输入
        expectError(t, "{", "未闭合对象");
        expectError(t, "[1,]", "数组尾逗号");
        expectError(t, "{\"a\" 1}", "对象缺冒号");
        expectError(t, "tru", "截断字面量");
        expectError(t, "\"\\u12\"", "残缺 unicode 转义");
        expectError(t, "01", "前导零非法");
        expectError(t, "12x", "多余字符");

        return t.report();
    }

    private static void expectError(TestHarness t, String bad, String label) {
        try {
            JsonParser.parse(bad);
            t.fail(label + "：应当抛出 JsonException");
        } catch (JsonException expected) {
            t.check(true, label);
        }
    }
}
