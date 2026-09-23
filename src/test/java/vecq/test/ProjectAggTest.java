package vecq.test;

import java.util.List;
import java.util.Map;

/**
 * 投影与聚合综合测试：
 *  - 投影保持列顺序、NULL 透传、稀疏选择下不混入未选中行；
 *  - 全局聚合 / 分组聚合在过滤后子集上的结果；
 *  - count(*) 计数选择向量行数（含重复）。
 */
public final class ProjectAggTest {

    private static final String TABLE = """
            "table":{"name":"orders","columns":[
              {"name":"id","type":"int","values":[10,20,30,null,50,60]},
              {"name":"amt","type":"int","values":[100,null,300,400,null,600]},
              {"name":"status","type":"string","values":["NEW","PAID","NEW",null,"PAID","NEW"]}
            ]}""";

    public static void run() {
        // 投影：status='NEW' -> 行 0,2,5；投影 [id, amt, status]
        Map<String, Object> r = Q.resp("{" + TABLE
                + ",\"filter\":{\"column\":\"status\",\"op\":\"=\",\"value\":\"NEW\"}"
                + ",\"projection\":[\"id\",\"amt\",\"status\"],\"batchSize\":2}");
        assertCol(r, "id", List.of(10, 30, 60), "投影 id");
        assertCol(r, "amt", List.of(100, 300, 600), "投影 amt");
        assertCol(r, "status", List.of("NEW", "NEW", "NEW"), "投影 status");

        // 投影全选（含 NULL 行）：NULL 必须正确出现在列式结果中
        Map<String, Object> all = Q.resp("{" + TABLE + ",\"projection\":[\"id\",\"amt\"]}");
        assertCol(all, "id", java.util.Arrays.asList(10, 20, 30, null, 50, 60), "全选投影 id（含 null）");
        assertCol(all, "amt", java.util.Arrays.asList(100, null, 300, 400, null, 600),
                "全选投影 amt（含 null，null 与行 3 的非 null 400 不串位）");

        // 全局聚合：全表
        Map<String, Object> agg = Q.resp("{" + TABLE
                + ",\"aggregates\":["
                + "{\"func\":\"count\",\"column\":\"*\"},"
                + "{\"func\":\"count\",\"column\":\"amt\"},"
                + "{\"func\":\"sum\",\"column\":\"amt\"},"
                + "{\"func\":\"avg\",\"column\":\"amt\"},"
                + "{\"func\":\"min\",\"column\":\"amt\"},"
                + "{\"func\":\"max\",\"column\":\"amt\"},"
                + "{\"func\":\"min\",\"column\":\"status\"},"
                + "{\"func\":\"max\",\"column\":\"status\"}"
                + "]}");
        Map<String, Object> row = globalRow(agg);
        Assert.eq(6L, row.get("count(*)"), "count(*) = 6");
        Assert.eq(4L, row.get("count(amt)"), "count(amt) 忽略 2 个 NULL = 4");
        Assert.eq(1400L, row.get("sum(amt)"), "sum(amt)=100+300+400+600=1400");
        Assert.eqDouble(350.0, ((Number) row.get("avg(amt)")).doubleValue(), "avg(amt)=350.0");
        Assert.eq(100, row.get("min(amt)"), "min(amt)=100");
        Assert.eq(600, row.get("max(amt)"), "max(amt)=600");
        Assert.eq("NEW", row.get("min(status)"), "min(status) 字典序 NEW");
        Assert.eq("PAID", row.get("max(status)"), "max(status) 字典序 PAID");

        // 聚合作用于过滤后的子集：status='PAID' -> 行 1(amt NULL),4(amt NULL)
        Map<String, Object> paid = Q.resp("{" + TABLE
                + ",\"filter\":{\"column\":\"status\",\"op\":\"=\",\"value\":\"PAID\"}"
                + ",\"aggregates\":[\"count(*)\",\"count(amt)\",\"sum(amt)\"]}");
        Map<String, Object> pr = globalRow(paid);
        Assert.eq(2L, pr.get("count(*)"), "过滤后 count(*)=2");
        Assert.eq(0L, pr.get("count(amt)"), "PAID 行 amt 全 NULL -> count(amt)=0");
        Assert.eq(null, pr.get("sum(amt)"), "全 NULL 子集 sum=null");

        // 分组聚合：按 status（NULL 自成一组），按组键首次出现顺序 NEW,PAID,NULL
        Map<String, Object> grp = Q.resp("{" + TABLE
                + ",\"groupBy\":[\"status\"],\"aggregates\":[\"count(*)\",\"sum(id)\"]}");
        @SuppressWarnings("unchecked")
        Map<String, Object> gagg = (Map<String, Object>) grp.get("aggregates");
        Assert.eq("grouped", gagg.get("kind"), "分组 kind");
        Assert.eq(List.of("status"), gagg.get("groupColumns"), "groupColumns");
        @SuppressWarnings("unchecked")
        List<Map<String, Object>> grows = (List<Map<String, Object>>) gagg.get("rows");
        Assert.eqInt(3, grows.size(), "3 个组（含 NULL 组）");
        Assert.eq("NEW", grows.get(0).get("status"), "第一组 NEW");
        Assert.eq(3L, grows.get(0).get("count(*)"), "NEW 组 3 行");
        Assert.eq(100L, grows.get(0).get("sum(id)"), "NEW 组 sum(id)=10+30+60");
        Assert.eq("PAID", grows.get(1).get("status"), "第二组 PAID");
        Assert.eq(70L, grows.get(1).get("sum(id)"), "PAID 组 sum(id)=20+50");
        Assert.eq(null, grows.get(2).get("status"), "第三组键为 NULL");
        Assert.eq(1L, grows.get(2).get("count(*)"), "NULL 组 1 行");

        // 聚合输出别名
        Map<String, Object> aliased = Q.resp("{" + TABLE
                + ",\"aggregates\":[{\"func\":\"sum\",\"column\":\"amt\",\"alias\":\"total\"}]}");
        Assert.that(globalRow(aliased).containsKey("total"), "聚合别名 total 生效");
    }

    @SuppressWarnings("unchecked")
    private static void assertCol(Map<String, Object> resp, String col,
                                  List<Object> expected, String name) {
        Map<String, Object> proj = (Map<String, Object>) resp.get("projection");
        Map<String, Object> cols = (Map<String, Object>) proj.get("columns");
        Assert.eq(expected, cols.get(col), name);
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> globalRow(Map<String, Object> resp) {
        Map<String, Object> agg = (Map<String, Object>) resp.get("aggregates");
        return (Map<String, Object>) agg.get("row");
    }
}
