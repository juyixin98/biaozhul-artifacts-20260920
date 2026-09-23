package vecq.test;

import vecq.Column;
import vecq.IntColumn;
import vecq.InvalidQueryException;
import vecq.Json;
import vecq.StringColumn;
import vecq.Table;

import java.util.List;
import java.util.Map;

/** 列与表的构造、NULL 语义、长度一致性与 JSON 往返。 */
public final class ColumnTableTest {

    public static void run() {
        Table t = Data.orders();
        Assert.eqInt(6, t.rowCount(), "orders 表 6 行");
        Column id = t.column("id");
        Assert.that(id instanceof IntColumn, "id 是整型列");
        Assert.that(id.isNull(3) && !id.isNull(0), "id 第 3 行 NULL，其余非 NULL");

        Column status = t.column("status");
        Assert.that(status instanceof StringColumn, "status 是字符串列");
        Object[] gathered = status.gather(new int[]{0, 3, 3, 5});
        Assert.eq(java.util.Arrays.asList("NEW", null, null, "NEW"), java.util.Arrays.asList(gathered),
                "gather 命中 NULL 返回 null 且重复下标重复取值");

        // values 中显式 null
        Object json = Json.parse("""
                {"name":"t","columns":[
                  {"name":"a","type":"int","values":[1,null,3]}
                ]}
                """);
        Table t2 = Table.fromJson(json);
        Assert.that(t2.column("a").isNull(1) && !t2.column("a").isNull(0),
                "values 中的 null 等价于 NULL 位图声明");

        // nullRows 位图方式
        Object json2 = Json.parse("""
                {"name":"t","columns":[
                  {"name":"a","type":"int","values":[1,2,3],"nullRows":[1]}
                ]}
                """);
        Table t3 = Table.fromJson(json2);
        Assert.that(t3.column("a").isNull(1), "nullRows 位图方式判 NULL 正确");

        // 两种表达不一致应报错
        String inconsistent = """
                {"name":"t","columns":[
                  {"name":"a","type":"int","values":[1,null,3],"nullRows":[0]}
                ]}
                """;
        Assert.fails(() -> Table.fromJson(Json.parse(inconsistent)),
                InvalidQueryException.class, "values-null 与 nullRows 不一致被拒绝");

        // 列长度不一致
        String ragged = """
                {"name":"t","columns":[
                  {"name":"a","type":"int","values":[1,2]},
                  {"name":"b","type":"string","values":["x"]}
                ]}
                """;
        Assert.fails(() -> Table.fromJson(Json.parse(ragged)),
                InvalidQueryException.class, "列长度不一致被拒绝");

        // 类型不匹配
        String badType = """
                {"name":"t","columns":[
                  {"name":"a","type":"int","values":[1,"oops",3]}
                ]}
                """;
        Assert.fails(() -> Table.fromJson(Json.parse(badType)),
                InvalidQueryException.class, "整型列中出现字符串被拒绝");

        // 表序列化为 JSON 后字段完整
        Map<String, Object> exported = t.toJson();
        Assert.eq("orders", exported.get("name"), "导出表名");
        Assert.eqInt(6, ((Number) exported.get("rowCount")).intValue(), "导出 rowCount");
    }
}
