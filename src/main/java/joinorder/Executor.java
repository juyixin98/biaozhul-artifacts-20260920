package joinorder;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 单机内存执行器：真正执行优化器产生的计划。
 *
 *   hash-join      在等值键上构建 HashMap（支持一条边上的多个等值条件，键为组合键）
 *   cartesian-join 嵌套循环（无连接条件，语义为 R × S）
 *
 * 执行器内部行一律以规范键 "表名.列名" 存储，避免多表同名列冲突；
 * 对用户输出结果时可恢复裸列名（Engine 负责）。
 *
 * 执行后自底向上回填每个节点的实际行数、实际列 NDV 与实际累计代价，
 * 实际代价口径与估计一致：Σ(子代价) + 本节点输出行数。
 */
public final class Executor {

    /** 执行中间结果：行（规范键）+ 真实统计。 */
    public static final class Result {
        public final List<Map<String, Object>> rows;
        public final Stats stats;
        public Result(List<Map<String, Object>> rows, Stats stats) {
            this.rows = rows;
            this.stats = stats;
        }
    }

    private final Model model;

    public Executor(Model model) {
        this.model = model;
    }

    public Result execute(PlanNode plan) {
        if (plan instanceof ScanNode) return scan((ScanNode) plan);
        if (plan instanceof JoinNode) return join((JoinNode) plan);
        throw new EngineException("未知计划节点: " + plan.getClass());
    }

    private Result scan(ScanNode n) {
        Table t = model.table(n.tableIdx);
        List<Map<String, Object>> out = new ArrayList<>(t.rows.size());
        for (Map<String, Object> r : t.rows) {
            Map<String, Object> q = new LinkedHashMap<>();
            for (Map.Entry<String, Object> e : r.entrySet()) {
                q.put(ColumnRef.of(t.name, e.getKey()).canonical, e.getValue());
            }
            out.add(q);
        }
        Stats s = t.actualStats.copy();
        n.actual = s;
        n.actualCost = s.rowCount;
        return new Result(out, s);
    }

    private Result join(JoinNode n) {
        Result lr = execute(n.left);
        Result rr = execute(n.right);

        List<Map<String, Object>> out;
        if (n.edgeIds.isEmpty()) {
            out = nestedLoop(lr.rows, rr.rows);
        } else {
            List<EqPredicate> preds = new ArrayList<>();
            for (Edge e : n.edges(model)) preds.addAll(e.predicates);
            out = hashJoin(lr, rr, preds, n.left.mask, n.right.mask);
        }

        Stats s = computeActualStats(out, n.mask);
        n.actual = s;
        n.actualCost = n.left.actualCost + n.right.actualCost + s.rowCount;
        return new Result(out, s);
    }

    private List<Map<String, Object>> nestedLoop(List<Map<String, Object>> left,
                                                 List<Map<String, Object>> right) {
        List<Map<String, Object>> out = new ArrayList<>(Math.max(16, left.size() * right.size()));
        for (Map<String, Object> l : left) {
            for (Map<String, Object> r : right) {
                Map<String, Object> merged = new LinkedHashMap<>(l);
                merged.putAll(r); // 键均带表名前缀，不会互相覆盖
                out.add(merged);
            }
        }
        return out;
    }

    private List<Map<String, Object>> hashJoin(Result lr, Result rr,
                                               List<EqPredicate> preds,
                                               int leftMask, int rightMask) {
        boolean buildLeft = lr.rows.size() <= rr.rows.size();
        Result build = buildLeft ? lr : rr;
        Result probe = buildLeft ? rr : lr;
        int buildMask = buildLeft ? leftMask : rightMask;
        int otherMask = buildMask == leftMask ? rightMask : leftMask;

        List<ColumnRef> buildKeys = new ArrayList<>();
        List<ColumnRef> probeKeys = new ArrayList<>();
        for (EqPredicate p : preds) {
            ColumnRef bc = p.columnOnSide(buildMask, model);
            ColumnRef pc = p.columnOnSide(otherMask, model);
            if (bc == null || pc == null) {
                throw new EngineException("内部错误：连接谓词与计划两侧不匹配: " + p);
            }
            buildKeys.add(bc);
            probeKeys.add(pc);
        }

        Map<List<Object>, List<Map<String, Object>>> table = new LinkedHashMap<>();
        for (Map<String, Object> row : build.rows) {
            List<Object> key = keyOf(row, buildKeys);
            if (containsNull(key)) continue; // null 不参与等值连接（SQL 三值逻辑）
            table.computeIfAbsent(key, k -> new ArrayList<>()).add(row);
        }

        List<Map<String, Object>> out = new ArrayList<>();
        for (Map<String, Object> prow : probe.rows) {
            List<Object> key = keyOf(prow, probeKeys);
            if (containsNull(key)) continue;
            List<Map<String, Object>> matches = table.get(key);
            if (matches == null) continue;
            for (Map<String, Object> brow : matches) {
                Map<String, Object> merged = new LinkedHashMap<>(brow);
                merged.putAll(prow);
                out.add(merged);
            }
        }
        return out;
    }

    private List<Object> keyOf(Map<String, Object> row, List<ColumnRef> keys) {
        List<Object> k = new ArrayList<>(keys.size());
        for (ColumnRef c : keys) k.add(row.get(c.canonical));
        return k;
    }

    private boolean containsNull(List<Object> key) {
        for (Object o : key) if (o == null) return true;
        return false;
    }

    /** 从实际输出（规范键）计算行数与各列 NDV。 */
    private Stats computeActualStats(List<Map<String, Object>> rows, int mask) {
        Map<String, Map<Object, Boolean>> distinct = new LinkedHashMap<>();
        int bits = mask;
        while (bits != 0) {
            int b = bits & -bits;
            Table t = model.table(Integer.numberOfTrailingZeros(b));
            for (String col : t.columns) {
                distinct.put(ColumnRef.of(t.name, col).canonical, new LinkedHashMap<>());
            }
            bits ^= b;
        }
        for (Map<String, Object> row : rows) {
            for (Map.Entry<String, Object> e : row.entrySet()) {
                distinct.computeIfAbsent(e.getKey(), k -> new LinkedHashMap<>())
                        .put(e.getValue(), Boolean.TRUE);
            }
        }
        Stats s = new Stats(rows.size());
        for (Map.Entry<String, Map<Object, Boolean>> e : distinct.entrySet()) {
            s.ndv.put(e.getKey(), (double) e.getValue().size());
        }
        return s;
    }
}
