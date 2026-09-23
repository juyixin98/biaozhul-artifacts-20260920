package vecq.test;

import java.util.List;
import java.util.Map;

/**
 * 重复选择测试（核心验收项）：
 *  - 显式 selection 中允许重复下标；
 *  - 顶层 union 的多个分支命中同一行时产生重复下标；
 *  - 投影按多重集输出（重复行重复出现）；
 *  - 聚合按多重集累计（count 翻倍、sum 重复加）。
 */
public final class DuplicateSelectionTest {

    private static final String TABLE = """
            "table":{"name":"t","columns":[
              {"name":"id","type":"int","values":[10,20,30]},
              {"name":"s","type":"string","values":["a","b","c"]}
            ]}""";

    public static void run() {
        // 1) 显式重复 selection：[2,0,2] 排序为 [0,2,2]
        Map<String, Object> r1 = Q.resp("{" + TABLE
                + ",\"selection\":[2,0,2],\"projection\":[\"id\",\"s\"],\"batchSize\":2}");
        Assert.eq(List.of(0, 2, 2), r1.get("selectedRows"), "显式重复下标保留并排序");
        assertColumn(r1, "id", List.of(10, 30, 30), "重复选择投影 id 出现两次 30");
        assertColumn(r1, "s", List.of("a", "c", "c"), "重复选择投影 s 出现两次 c");

        // 下游批数按选择向量长度计算（3 行，bs=2 -> 2 批）
        assertDownstreamBatches(r1, 2, "重复选择的下游批数按 SV 长度切");

        // 2) 过滤只保留重复源中的部分行：id>=20 -> [2,2]
        Map<String, Object> r2 = Q.resp("{" + TABLE
                + ",\"selection\":[2,0,2],\"filter\":{\"column\":\"id\",\"op\":\">=\",\"value\":20}"
                + ",\"projection\":[\"id\"]}");
        Assert.eq(List.of(2, 2), r2.get("selectedRows"), "过滤重复输入时重复输出");
        assertColumn(r2, "id", List.of(30, 30), "投影仍为多重集");

        // 3) union：id<20 命中行0；id>10 命中行1,2；union => [0,1,2]
        //    再让两分支都命中行2：分支A id<=30（0,1,2），分支B id>=20（1,2）=> [0,1,1,2,2]
        Map<String, Object> r3 = Q.resp("{" + TABLE
                + ",\"filter\":{\"op\":\"union\",\"branches\":["
                + "{\"column\":\"id\",\"op\":\"<=\",\"value\":30},"
                + "{\"column\":\"id\",\"op\":\">=\",\"value\":20}"
                + "]},\"projection\":[\"id\"]}");
        Assert.eq(List.of(0, 1, 1, 2, 2), r3.get("selectedRows"),
                "union 多集合并：行1、行2 各重复一次");
        assertColumn(r3, "id", List.of(10, 20, 20, 30, 30), "union 投影多重集");

        // 4) union + 聚合：count(*) = 5；sum(id) = 10+20+20+30+30 = 110
        Map<String, Object> r4 = Q.resp("{" + TABLE
                + ",\"filter\":{\"op\":\"union\",\"branches\":["
                + "{\"column\":\"id\",\"op\":\"<=\",\"value\":30},"
                + "{\"column\":\"id\",\"op\":\">=\",\"value\":20}"
                + "]},\"aggregates\":[\"count(*)\",\"sum(id)\",\"avg(id)\",\"min(id)\",\"max(id)\"]}");
        @SuppressWarnings("unchecked")
        Map<String, Object> aggRow = (Map<String, Object>)
                ((Map<String, Object>) r4.get("aggregates")).get("row");
        Assert.eq(5L, aggRow.get("count(*)"), "union 后 count(*) 按多重集计数");
        Assert.eq(110L, aggRow.get("sum(id)"), "union 后 sum 重复累加");
        Assert.eqDouble(22.0, ((Number) aggRow.get("avg(id)")).doubleValue(),
                "union 后 avg = 110/5");
        Assert.eq(10, aggRow.get("min(id)"), "min 不受重复影响");
        Assert.eq(30, aggRow.get("max(id)"), "max 不受重复影响");

        // 5) 对照：同样两个谓词用普通 OR，每行只保留一次
        Map<String, Object> r5 = Q.resp("{" + TABLE
                + ",\"filter\":{\"op\":\"or\",\"children\":["
                + "{\"column\":\"id\",\"op\":\"<=\",\"value\":30},"
                + "{\"column\":\"id\",\"op\":\">=\",\"value\":20}"
                + "]}}");
        Assert.eq(List.of(0, 1, 2), r5.get("selectedRows"),
                "普通 OR 不去重语义：每行至多一次（与 union 形成对照）");
    }

    @SuppressWarnings("unchecked")
    private static void assertColumn(Map<String, Object> resp, String col,
                                     List<Object> expected, String name) {
        Map<String, Object> proj = (Map<String, Object>) resp.get("projection");
        Map<String, Object> cols = (Map<String, Object>) proj.get("columns");
        Assert.eq(expected, cols.get(col), name);
    }

    @SuppressWarnings("unchecked")
    private static void assertDownstreamBatches(Map<String, Object> resp, int expected, String name) {
        Map<String, Object> exec = (Map<String, Object>) resp.get("execution");
        Map<String, Object> vec = (Map<String, Object>) exec.get("vector");
        Assert.eqInt(expected, ((Number) vec.get("downstreamBatches")).intValue(), name);
    }
}
