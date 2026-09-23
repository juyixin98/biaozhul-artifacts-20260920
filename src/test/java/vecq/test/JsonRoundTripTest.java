package vecq.test;

import vecq.IntColumn;
import vecq.Json;
import vecq.NullBitmap;
import vecq.StringColumn;
import vecq.Table;

import java.util.List;
import java.util.Map;

/** 手写 JSON 解析器 / 序列化器的往返与边界测试。 */
public final class JsonRoundTripTest {

    public static void run() {
        // 基本类型 + 顺序保留 + 嵌套
        String s = "{\"b\":true,\"n\":null,\"i\":-3,\"d\":1.25e2,\"s\":\"a\\nb\\t\\u0041\","
                + "\"arr\":[1,2,[3]],\"o\":{\"x\":false}}";
        Map<String, Object> m = Json.asMap(Json.parse(s));
        Assert.eq(Boolean.TRUE, m.get("b"), "true 解析");
        Assert.that(m.get("n") == null, "null 解析");
        Assert.eq(-3L, ((Number) m.get("i")).longValue(), "负整数解析");
        Assert.eqDouble(125.0, ((Number) m.get("d")).doubleValue(), "科学计数法解析");
        Assert.eq("a\nb\tA", m.get("s"), "转义与 \\u0041 解析");
        Assert.eq(List.of(1L, 2L, List.of(3L)), m.get("arr"), "嵌套数组解析");
        Assert.eq(false, ((Map<?, ?>) m.get("o")).get("x"), "嵌套对象解析");

        // 序列化往返
        Object reparsed = Json.parse(Json.write(m));
        Assert.eq(Json.write(m), Json.write(reparsed), "紧凑序列化往返一致");

        // 表 JSON 往返
        Table t = new Table("t", List.of(
                new IntColumn("id", new int[]{1, 0, 3}, NullBitmap.fromNullRows(3, new int[]{1})),
                new StringColumn("s", new String[]{"x", null, "z"},
                        NullBitmap.fromNullRows(3, new int[]{1}))));
        Map<String, Object> exported = t.toJson();
        Table back = Table.fromJson(Json.parse(Json.write(exported)));
        Assert.eqInt(3, back.rowCount(), "往返表行数");
        Assert.that(back.column("id").isNull(1) && back.column("s").isNull(1),
                "往返后 NULL 位图保留");

        // 错误 JSON
        Assert.fails(() -> Json.parse("{\"a\":}"), Json.JsonException.class, "缺值被拒");
        Assert.fails(() -> Json.parse("[1,2,]"), Json.JsonException.class, "尾随逗号被拒");
        Assert.fails(() -> Json.parse("\"unterminated"), Json.JsonException.class,
                "未闭合字符串被拒");
        Assert.fails(() -> Json.parse("{\"a\":1} extra"), Json.JsonException.class,
                "尾部多余字符被拒");

        // pretty 输出可再解析
        Object prettyBack = Json.parse(Json.writePretty(m));
        Assert.eq(Json.write(m), Json.write(prettyBack), "pretty 输出可往返");
    }
}
