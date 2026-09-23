package vecq.test;

import java.util.List;
import java.util.Map;

/**
 * 全空列测试：整列 NULL 时，
 *  - 比较谓词全部 UNKNOWN（不命中）；
 *  - IS NULL 全部命中；IS NOT NULL 全部不命中；
 *  - 聚合 count(*)=行数、count(col)=0、sum/avg/min/max=null；
 *  - 投影与分组中 NULL 正确透传。
 */
public final class AllNullColumnTest {

    private static String table(int n) {
        StringBuilder vals = new StringBuilder();
        for (int i = 0; i < n; i++) {
            if (i > 0) vals.append(',');
            vals.append("null");
        }
        return "{\"name\":\"an\",\"columns\":["
                + "{\"name\":\"id\",\"type\":\"int\",\"values\":[" + vals + "]},"
                + "{\"name\":\"note\",\"type\":\"string\",\"values\":[" + vals + "]}]}";
    }

    public static void run() {
        String t = table(5);

        Assert.eq(List.of(),
                Q.rows("{\"table\":" + t + ",\"filter\":{\"column\":\"id\",\"op\":\">\",\"value\":0}}"),
                "全 NULL 整型列比较无命中");
        Assert.eq(List.of(0, 1, 2, 3, 4),
                Q.rows("{\"table\":" + t + ",\"filter\":{\"op\":\"isNull\",\"column\":\"id\"}}"),
                "全 NULL 整型列 IS NULL 全命中");
        Assert.eq(List.of(),
                Q.rows("{\"table\":" + t + ",\"filter\":{\"op\":\"isNotNull\",\"column\":\"note\"}}"),
                "全 NULL 字符串列 IS NOT NULL 无命中");
        Assert.eq(List.of(),
                Q.rows("{\"table\":" + t + ",\"filter\":{\"op\":\"not\",\"child\":{\"op\":\"isNull\",\"column\":\"id\"}}}"),
                "NOT(IS NULL) 在全 NULL 列上无命中");

        // 聚合
        Map<String, Object> resp = Q.resp("{\"table\":" + t + ",\"aggregates\":["
                + "\"count(*)\",\"count(id)\",\"sum(id)\",\"avg(id)\",\"min(id)\",\"max(note)\""
                + "]}");
        @SuppressWarnings("unchecked")
        Map<String, Object> agg = (Map<String, Object>) resp.get("aggregates");
        Map<String, Object> row = oneRow(agg);
        AssertEq(row, "count(*)", 5L);
        AssertEq(row, "count(id)", 0L);
        AssertEq(row, "sum(id)", null);
        AssertEq(row, "avg(id)", null);
        AssertEq(row, "min(id)", null);
        AssertEq(row, "max(note)", null);

        // 全 NULL 列分组：所有行属于同一个（NULL 键）组
        Map<String, Object> grp = Q.resp("{\"table\":" + t
                + ",\"groupBy\":[\"note\"],\"aggregates\":[\"count(*)\",\"count(id)\"]}");
        @SuppressWarnings("unchecked")
        Map<String, Object> gagg = (Map<String, Object>) grp.get("aggregates");
        Assert.eq("grouped", gagg.get("kind"), "分组聚合 kind=grouped");
        @SuppressWarnings("unchecked")
        List<Map<String, Object>> rows = (List<Map<String, Object>>) gagg.get("rows");
        Assert.eqInt(1, rows.size(), "全 NULL 分组列只产生一个组");
        AssertEq(rows.get(0), "note", null);
        AssertEq(rows.get(0), "count(*)", 5L);
        AssertEq(rows.get(0), "count(id)", 0L);

        // 空表上的全局聚合约定
        String empty = "{\"name\":\"e\",\"columns\":[{\"name\":\"id\",\"type\":\"int\",\"values\":[]}]}";
        Map<String, Object> ea = Q.resp("{\"table\":" + empty
                + ",\"aggregates\":[\"count(*)\",\"sum(id)\",\"avg(id)\"]}");
        @SuppressWarnings("unchecked")
        Map<String, Object> eagg = (Map<String, Object>) ea.get("aggregates");
        Map<String, Object> erow = oneRow(eagg);
        AssertEq(erow, "count(*)", 0L);
        AssertEq(erow, "sum(id)", null);
        AssertEq(erow, "avg(id)", null);
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> oneRow(Map<String, Object> agg) {
        return (Map<String, Object>) agg.get("row");
    }

    private static void AssertEq(Map<String, Object> row, String key, Object expected) {
        Assert.eq(expected, row.get(key), "全 NULL 聚合 " + key);
    }
}
