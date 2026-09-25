package streamagg.tests;

import java.math.BigDecimal;
import java.util.List;
import java.util.Map;

import streamagg.json.JsonParser;
import streamagg.json.JsonWriter;

/** JSON 解析/序列化往返测试，重点验证数字精度与转义。 */
public final class JsonTest {

    public static void main(String[] args) {
        TestFramework t = new TestFramework();

        t.test("数字解析为 BigDecimal 且保留精度", () -> {
            Object v = JsonParser.parse("{\"a\":0.1,\"b\":123456789.123456789,\"c\":-2,\"d\":1e3}");
            @SuppressWarnings("unchecked")
            Map<String, Object> m = (Map<String, Object>) v;
            t.eq(((BigDecimal) m.get("a")).compareTo(new BigDecimal("0.1")), 0, "0.1");
            t.eq(((BigDecimal) m.get("b")).compareTo(new BigDecimal("123456789.123456789")), 0,
                    "长小数精度");
            t.eq(((BigDecimal) m.get("c")).intValue(), -2, "负数");
            t.eq(((BigDecimal) m.get("d")).intValue(), 1000, "科学计数法");
        });

        t.test("往返：对象/数组/字符串转义/中文", () -> {
            String json = "{\"msg\":\"a\\nb\\t\\u4e2d\\\"文\\\\\",\"arr\":[1,true,null,-0.5],\"empty\":{}}";
            Object parsed = JsonParser.parse(json);
            String out = JsonWriter.write(parsed);
            Object reparsed = JsonParser.parse(out);
            t.eq(reparsed, parsed, "二次解析相等");
            @SuppressWarnings("unchecked")
            Map<String, Object> m = (Map<String, Object>) reparsed;
            t.eq(m.get("msg"), "a\nb\t中\"文\\", "转义字符正确");
            t.check(m.get("arr") instanceof List<?> && ((List<?>) m.get("arr")).size() == 4,
                    "数组保留");
        });

        t.test("非法 JSON 抛 JsonException", () -> {
            t.expectThrows(RuntimeException.class, "输入结尾",
                    () -> JsonParser.parse("{\"a\":1"), "未闭合对象");
            t.expectThrows(RuntimeException.class, "多余字符",
                    () -> JsonParser.parse("{}x"), "尾随字符");
            t.expectThrows(RuntimeException.class, "数字",
                    () -> JsonParser.parse("{\"a\":-}"), "非法数字");
        });

        t.test("pretty 输出可读且可再解析", () -> {
            Object obj = JsonParser.parse("{\"k\":[1,2],\"n\":null}");
            String pretty = JsonWriter.pretty(obj);
            t.check(pretty.contains("\n"), "pretty 含换行");
            Object again = JsonParser.parse(pretty);
            t.eq(again, obj, "pretty 往返一致");
        });

        int code = t.summary("JsonTest");
        if (code != 0) {
            System.exit(code);
        }
    }
}
