package vecq.test;

import java.util.List;
import java.util.Map;

/**
 * 批边界测试：固定数据与过滤条件，遍历 batchSize = 1..n+2，
 * 结果必须逐下标一致，且实际过滤批数等于 ceil(输入行数 / batchSize)。
 *
 * 测试数据 v（13 行，质数长度制造各种余数边界）：
 *   偶数行 = NULL，奇数行 = 行号，即 [NULL,1,NULL,3,NULL,5,NULL,7,NULL,9,NULL,11,NULL]
 */
public final class BatchBoundaryTest {

    private static final int N = 13;

    private static String tableDef() {
        StringBuilder vals = new StringBuilder();
        for (int i = 0; i < N; i++) {
            if (i > 0) vals.append(',');
            vals.append(i % 2 == 0 ? "null" : i);
        }
        return "{\"name\":\"big\",\"columns\":[{\"name\":\"v\",\"type\":\"int\",\"values\":["
                + vals + "]}]}";
    }

    public static void run() {
        String table = tableDef();
        // v >= 2 AND v <= 10 AND v IS NOT NULL -> 行 3,5,7,9
        List<Object> expected = List.of(3, 5, 7, 9);
        String wide = "{\"table\":" + table + ",\"filter\":{\"op\":\"and\",\"children\":["
                + "{\"op\":\"isNotNull\",\"column\":\"v\"},"
                + "{\"column\":\"v\",\"op\":\">=\",\"value\":2},"
                + "{\"column\":\"v\",\"op\":\"<=\",\"value\":10}"
                + "]}}";

        for (int bs = 1; bs <= N + 2; bs++) {
            String req = withBatch(wide, bs);
            Map<String, Object> resp = Q.resp(req);
            Assert.eq(expected, resp.get("selectedRows"),
                    "批大小 " + bs + " 下命中行稳定为 " + expected);
            int expectedBatches = (N + bs - 1) / bs;
            int actualBatches = vectorStat(resp, "filterBatches");
            Assert.eqInt(expectedBatches, actualBatches,
                    "批大小 " + bs + " 时过滤批数为 ceil(" + N + "/" + bs + ")=" + expectedBatches);
        }

        // 批次特别大（全部行落在同一批）
        Map<String, Object> one = Q.resp(withBatch(wide, 100));
        Assert.eq(expected, one.get("selectedRows"), "batchSize 远大于行数时单批处理");
        Assert.eqInt(1, vectorStat(one, "filterBatches"), "单批处理时 filterBatches=1");

        // 空结果但输入非空：仍处理 1 个批次
        String none = "{\"table\":" + table + ",\"batchSize\":99,\"filter\":"
                + "{\"column\":\"v\",\"op\":\">\",\"value\":10000}}";
        Map<String, Object> nr = Q.resp(none);
        Assert.eq(List.of(), nr.get("selectedRows"), "不可能命中的谓词产出空选择向量");
        Assert.eqInt(1, vectorStat(nr, "filterBatches"), "空结果仍处理 1 个批次");

        // 空表：0 行 0 批
        String empty = "{\"table\":{\"name\":\"e\",\"columns\":[{\"name\":\"v\",\"type\":\"int\",\"values\":[]}]},"
                + "\"filter\":{\"column\":\"v\",\"op\":\">\",\"value\":0}}";
        Map<String, Object> er = Q.resp(empty);
        Assert.eq(List.of(), er.get("selectedRows"), "空表过滤结果为空");
        Assert.eqInt(0, vectorStat(er, "filterBatches"), "空表 0 个过滤批次");
    }

    @SuppressWarnings("unchecked")
    static int vectorStat(Map<String, Object> resp, String key) {
        Map<String, Object> exec = (Map<String, Object>) resp.get("execution");
        Map<String, Object> vec = (Map<String, Object>) exec.get("vector");
        return ((Number) vec.get(key)).intValue();
    }

    private static String withBatch(String req, int bs) {
        return req.replace("\"filter\"", "\"batchSize\":" + bs + ",\"filter\"");
    }
}
