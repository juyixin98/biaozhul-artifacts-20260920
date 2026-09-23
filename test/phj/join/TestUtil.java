package phj.join;

import phj.core.JoinType;
import phj.core.Key;
import phj.core.QueryRequest;
import phj.core.QueryResult;
import phj.core.Relation;
import phj.core.Row;
import phj.core.Value;

import java.util.ArrayList;
import java.util.Collection;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/** 测试公共工具：造数据、跑引擎、跑参考实现、多重集比对。 */
public final class TestUtil {

    private TestUtil() {}

    // ----------------------------------------------------------- 造数据

    public static Relation relation(String name, List<String> cols, Object[]... rows) {
        List<Row> rs = new ArrayList<>();
        for (Object[] r : rows) rs.add(row(r));
        return new Relation(name, cols, rs);
    }

    public static Row row(Object... values) {
        List<Value> vs = new ArrayList<>(values.length);
        for (Object o : values) vs.add(Value.fromJson(o));
        return new Row(vs);
    }

    /** 随机表：键取自 0..distinctKeys-1，按 nullPct 注入 NULL，含字符串/数值混合类型可选。 */
    public static Relation randomRelation(String name, int n, int distinctKeys,
                                          int nullPct, java.util.Random rnd,
                                          boolean mixedTypes, int payloadWidth) {
        List<String> cols = new ArrayList<>();
        cols.add("id");
        cols.add("k");
        if (payloadWidth > 0) cols.add("payload");
        List<Row> rows = new ArrayList<>(n);
        for (int i = 0; i < n; i++) {
            Object key;
            boolean isNull = rnd.nextInt(100) < nullPct;
            if (isNull) {
                key = null;
            } else if (mixedTypes && rnd.nextInt(100) < 30) {
                // 30% 概率用字符串形式的数字（与数值不相等，制造跨型不匹配）
                key = "s" + rnd.nextInt(distinctKeys);
            } else if (mixedTypes && rnd.nextInt(100) < 10) {
                key = (double) rnd.nextInt(distinctKeys);
            } else {
                key = (long) rnd.nextInt(distinctKeys);
            }
            List<Object> vals = new ArrayList<>();
            vals.add((long) i);
            vals.add(key);
            if (payloadWidth > 0) vals.add(name + "-payload-" + i + "-" + "x".repeat(payloadWidth));
            rows.add(row(vals.toArray()));
        }
        return new Relation(name, cols, rows);
    }

    // ----------------------------------------------------------- 跑两种实现

    public static QueryRequest request(Relation left, Relation right, JoinType type,
                                       List<String> keys, long threshold,
                                       int partitions, long quotaBytes) {
        QueryRequest q = new QueryRequest();
        q.joinType = type;
        q.keyPairs = new ArrayList<>();
        for (String k : keys) q.keyPairs.add(new phj.core.JoinKeyPair(k, k));
        q.left = left;
        q.right = right;
        q.memoryThresholdRows = threshold;
        q.partitions = partitions;
        q.diskQuotaBytes = quotaBytes;
        q.spillDir = null;
        q.validate();
        return q;
    }

    public static QueryResult runEngine(QueryRequest req) {
        return new HashJoinEngine(req).execute();
    }

    public static List<Row> runReference(QueryRequest req) {
        int[][] idx = req.resolveKeyIndices(req.left, req.right);
        return NestedLoopJoin.join(req.left, req.right, idx[0], idx[1], req.joinType);
    }

    // ----------------------------------------------------------- 多重集比对

    /** 比较两个行集合作为多重集是否相同（顺序无关、重复次数敏感）。 */
    public static void assertMultisetEquals(Collection<Row> expected, Collection<Row> actual) {
        Map<Row, Integer> expCounts = new HashMap<>();
        Map<Row, Integer> actCounts = new HashMap<>();
        for (Row r : expected) expCounts.merge(r, 1, Integer::sum);
        for (Row r : actual) actCounts.merge(r, 1, Integer::sum);
        if (!expCounts.equals(actCounts)) {
            StringBuilder sb = new StringBuilder("多重集不匹配！\n");
            sb.append("期望行数=").append(expected.size()).append(" 实际行数=").append(actual.size()).append('\n');
            // 找差异
            Map<Row, Integer> missing = new HashMap<>(expCounts);
            actCounts.forEach((k, v) -> missing.merge(k, -v, (a, b) -> a - b));
            missing.entrySet().removeIf(e -> e.getValue() == 0);
            Map<Row, Integer> extra = new HashMap<>(actCounts);
            expCounts.forEach((k, v) -> extra.merge(k, -v, (a, b) -> a - b));
            extra.entrySet().removeIf(e -> e.getValue() == 0);
            int shown = 0;
            for (var e : missing.entrySet()) {
                if (shown++ >= 10) { sb.append("  ...（更多缺失省略）\n"); break; }
                sb.append("  缺失 x").append(e.getValue()).append(" : ").append(e.getKey()).append('\n');
            }
            shown = 0;
            for (var e : extra.entrySet()) {
                if (shown++ >= 10) { sb.append("  ...（更多多余省略）\n"); break; }
                sb.append("  多余 x").append(e.getValue()).append(" : ").append(e.getKey()).append('\n');
            }
            throw new AssertionError(sb.toString());
        }
    }

    /** 端到端：同一请求分别跑引擎与参考实现并比较。 */
    public static QueryResult assertAgainstReference(QueryRequest req) {
        List<Row> expected = runReference(req);
        QueryResult actual = runEngine(req);
        assertMultisetEquals(expected, actual.rows);
        // 输出行数与参考一致
        if (actual.rows.size() != expected.size()) {
            throw new AssertionError("行数不一致：" + expected.size() + " vs " + actual.rows.size());
        }
        return actual;
    }

    /** 从行中抽取键（供多重集断言之外的附加检查）。 */
    public static List<Key> keysOf(List<Row> rows, int[] idxs) {
        List<Key> ks = new ArrayList<>();
        for (Row r : rows) ks.add(Key.ofRow(r, idxs));
        return ks;
    }

    public static List<Object> valuesAsJson(QueryResult r) {
        List<Object> out = new ArrayList<>();
        for (Row row : r.rows) {
            List<Object> vals = new ArrayList<>();
            for (int i = 0; i < row.width(); i++) vals.add(row.get(i).toJson());
            out.add(vals);
        }
        return out;
    }
}
