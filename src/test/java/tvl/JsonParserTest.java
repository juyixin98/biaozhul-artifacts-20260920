package tvl;

import java.util.List;
import java.util.Map;

import tvl.json.Json;
import tvl.json.JsonParseException;

/** 手写 JSON 解析器 / 序列化器的往返与错误处理测试。 */
public class JsonParserTest {

    public static void run() {
        roundTrip();
        parseNumbers();
        parseErrors();
        prettyStable();
        escapes();
    }

    private static void roundTrip() {
        Map<String, Object> m = Json.parseObject(
                "{\"a\":1,\"b\":[true,false,null],\"c\":\"x\"}");
        TF.assertEquals(1L, m.get("a"));
        TF.assertEquals(true, ((List<?>) m.get("b")).get(0));
        TF.assertEquals("x", m.get("c"));
        TF.assertNull(m.get("missing"), "不存在的键应为 null");
    }

    private static void parseNumbers() {
        TF.assertEquals(0L, Json.parse("0"));
        TF.assertEquals(-123L, Json.parse("-123"));
        TF.assertEquals(9223372036854775807L, Json.parse("9223372036854775807"));
        TF.assertEquals(1.5d, Json.parse("1.5"));
        TF.assertEquals(1.0e10d, Json.parse("1e10"));
        // 超大整数（超出 long）应报错
        TF.assertThrows(JsonParseException.class, () -> Json.parse("99999999999999999999"));
    }

    private static void parseErrors() {
        TF.assertThrows(JsonParseException.class, () -> Json.parse(""));
        TF.assertThrows(JsonParseException.class, () -> Json.parse("{"));
        TF.assertThrows(JsonParseException.class, () -> Json.parse("[1,]"));
        TF.assertThrows(JsonParseException.class, () -> Json.parse("{\"a\"}"));
        TF.assertThrows(JsonParseException.class, () -> Json.parse("tru"));
        TF.assertThrows(JsonParseException.class, () -> Json.parse("{'a':1}"));
        TF.assertThrows(JsonParseException.class, () -> Json.parse("1 2"));
    }

    private static void prettyStable() {
        Object m = Json.parseObject("{\"x\":[1,2],\"y\":{\"z\":null}}");
        String pretty = Json.pretty(m);
        // 再解析一次应等价
        TF.assertEquals(m, Json.parse(pretty));
        // 紧凑与美化解析结果一致
        TF.assertEquals(m, Json.parse(Json.stringify(m)));
    }

    private static void escapes() {
        TF.assertEquals("a\"b\\c\n", Json.parse("\"a\\\"b\\\\c\\n\""));
        TF.assertEquals("A", Json.parse("\"\\u0041\""));
        TF.assertThrows(JsonParseException.class, () -> Json.parse("\"\\x\""));
        // 序列化转义
        TF.assertEquals("\"\\n\\t\\\"\"", Json.stringify("\n\t\""));
    }
}
