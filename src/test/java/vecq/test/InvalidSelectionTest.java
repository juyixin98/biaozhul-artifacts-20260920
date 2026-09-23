package vecq.test;

import vecq.InvalidQueryException;
import vecq.InvalidSelectionException;

/**
 * 无效选择下标测试：
 *  - 显式 selection 中的越界 / 负下标必须在构造期被拒绝；
 *  - 计划层面的非法请求也必须被拒绝（列不存在、类型不匹配、非法算子、union 嵌套等）。
 */
public final class InvalidSelectionTest {

    private static final String TABLE = """
            "table":{"name":"t","columns":[
              {"name":"id","type":"int","values":[1,2,3]},
              {"name":"s","type":"string","values":["a","b","c"]}
            ]}""";

    private static String sel(int... idx) {
        StringBuilder sb = new StringBuilder("{");
        sb.append(TABLE).append(",\"selection\":[");
        for (int i = 0; i < idx.length; i++) {
            if (i > 0) sb.append(',');
            sb.append(idx[i]);
        }
        return sb.append("]}").toString();
    }

    public static void run() {
        // 越界下标
        Assert.fails(() -> Q.run(sel(0, 3)), InvalidSelectionException.class,
                "下标 3（==行数）无效");
        Assert.fails(() -> Q.run(sel(99)), InvalidSelectionException.class,
                "下标 99 越界无效");
        Assert.fails(() -> Q.run(sel(-1)), InvalidSelectionException.class,
                "负下标无效");
        Assert.fails(() -> Q.run(sel(0, -2, 2)), InvalidSelectionException.class,
                "混合列表中的负下标无效");

        // 列不存在
        Assert.fails(() -> Q.run("{\"table\":{\"name\":\"t\",\"columns\":["
                + "{\"name\":\"id\",\"type\":\"int\",\"values\":[1,2]}]},"
                + "\"filter\":{\"column\":\"nope\",\"op\":\"=\",\"value\":1}}"),
                InvalidQueryException.class, "过滤引用不存在的列被拒绝");

        Assert.fails(() -> Q.run("{\"table\":{\"name\":\"t\",\"columns\":["
                + "{\"name\":\"id\",\"type\":\"int\",\"values\":[1,2]}]},"
                + "\"projection\":[\"nope\"]}"),
                InvalidQueryException.class, "投影不存在的列被拒绝");

        // 类型不匹配
        Assert.fails(() -> Q.run("{\"table\":{\"name\":\"t\",\"columns\":["
                + "{\"name\":\"id\",\"type\":\"int\",\"values\":[1,2]}]},"
                + "\"filter\":{\"column\":\"id\",\"op\":\"=\",\"value\":\"x\"}}"),
                InvalidQueryException.class, "整型列与字符串比较被拒绝");

        Assert.fails(() -> Q.run("{\"table\":{\"name\":\"t\",\"columns\":["
                + "{\"name\":\"s\",\"type\":\"string\",\"values\":[\"a\"]}]},"
                + "\"filter\":{\"column\":\"s\",\"op\":\"<\",\"value\":\"a\"}}"),
                InvalidQueryException.class, "字符串列使用 < 被拒绝");

        // 非法算子
        Assert.fails(() -> Q.run("{\"table\":{\"name\":\"t\",\"columns\":["
                + "{\"name\":\"id\",\"type\":\"int\",\"values\":[1,2]}]},"
                + "\"filter\":{\"column\":\"id\",\"op\":\"~~\",\"value\":1}}"),
                InvalidQueryException.class, "非法比较算子被拒绝");

        // sum 字符串列
        Assert.fails(() -> Q.run("{\"table\":{\"name\":\"t\",\"columns\":["
                + "{\"name\":\"s\",\"type\":\"string\",\"values\":[\"a\",\"b\"]}]},"
                + "\"aggregates\":[\"sum(s)\"]}"),
                InvalidQueryException.class, "sum 字符串列被拒绝");

        // union 嵌套
        Assert.fails(() -> Q.run("{\"table\":{\"name\":\"t\",\"columns\":["
                + "{\"name\":\"id\",\"type\":\"int\",\"values\":[1,2]}]},"
                + "\"filter\":{\"op\":\"and\",\"children\":["
                + "{\"op\":\"union\",\"branches\":["
                + "{\"column\":\"id\",\"op\":\"=\",\"value\":1}]}"
                + "]}}"),
                InvalidQueryException.class, "union 嵌在 and 内部被拒绝");

        // batchSize 非法
        Assert.fails(() -> Q.run("{\"table\":{\"name\":\"t\",\"columns\":["
                + "{\"name\":\"id\",\"type\":\"int\",\"values\":[1,2]}]},"
                + "\"batchSize\":0}"),
                InvalidQueryException.class, "batchSize=0 被拒绝");

        // JSON 语法错误由 Json 层抛出（不是 InvalidQuery，单独覆盖）
        Assert.fails(() -> vecq.Json.parse("{oops"), vecq.Json.JsonException.class,
                "畸形 JSON 抛出 JsonException");
    }
}
