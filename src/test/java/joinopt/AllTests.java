package joinopt;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 全套自动化测试（零依赖，直接 main 运行）。
 * 覆盖：JSON 往返、基数估计、DP 最优性、穷举一致性、断开图/笛卡尔积、
 * 执行器语义、倾斜失准、顺序不变性、NULL 语义、多谓词哈希连接、端到端入口、导出、输入校验。
 */
public final class AllTests {

    private static int passed = 0;
    private static final List<String> failed = new ArrayList<>();

    public static void main(String[] args) {
        String[] names = {
                "jsonParseAndSerializeRoundTrip",
                "jsonHandlesNumbersStringsNullEscape",
                "tableNdvComputedFromDataAndCapped",
                "cardinalityEstimationTextbookFormula",
                "estimationIsOrderIndependent",
                "dpPicksMinCostPlan_chain",
                "dpMatchesExhaustiveEnumeration",
                "legalTreeCountMatchesFormula",
                "disconnectedGraphMarksCartesianAndSemantics",
                "executorHashJoinSemantics",
                "executorMultiPredicateJoin",
                "nullKeysDoNotMatch",
                "numericStringCanonicalization",
                "skewedStatsEstimateVsActual",
                "nestedLoopCartesianSemantics",
                "allLegalPlansProduceIdenticalMultiset",
                "statsOnlySkipsExecution",
                "maxTablesLimit",
                "endToEndHandlerAndExport",
                "badRequestsFail",
                "zeroRowTable",
        };
        for (String name : names) {
            try {
                AllTests.class.getDeclaredMethod(name).invoke(null);
                passed++;
                System.out.println("PASS " + name);
            } catch (Throwable t) {
                Throwable cause = t.getCause() != null ? t.getCause() : t;
                failed.add(name);
                System.out.println("FAIL " + name + " :: " + cause);
            }
        }
        System.out.println();
        System.out.println("通过 " + passed + " / " + names.length);
        if (!failed.isEmpty()) {
            System.out.println("失败用例: " + failed);
            System.exit(1);
        }
    }

    // ---------- 1. JSON ----------

    static void jsonParseAndSerializeRoundTrip() {
        String s = "{\"a\":1,\"b\":[true,false,null,\"x y\"],\"c\":{\"d\":-2.5}}";
        Object v = Json.parse(s);
        Map<String, Object> m = Json.asObj(v);
        TestFramework.eq(Json.lng(m, "a"), 1L, "字段 a");
        TestFramework.eq(Json.asArr(m.get("b")).get(0), Boolean.TRUE, "布尔");
        TestFramework.eq(Json.asArr(m.get("b")).get(2), null, "null 字面量");
        String again = Json.pretty(v);
        Object v2 = Json.parse(again);
        TestFramework.check(v.equals(v2), "JSON 往返后结构应一致");
    }

    static void jsonHandlesNumbersStringsNullEscape() {
        Map<String, Object> m = TestFramework.obj("{\"n\":1,\"f\":1.0,\"s\":\"a\\tb\\nc\"}");
        TestFramework.check(m.get("n") instanceof Long, "整数解析为 Long");
        TestFramework.check(m.get("f") instanceof Double, "小数解析为 Double");
        TestFramework.eq(m.get("s"), "a\tb\nc", "转义字符");
        String pretty = Json.pretty(m);
        TestFramework.check(Json.parse(pretty).equals(m), "转义后往返一致");
    }

    // ---------- 2. 表统计 ----------

    static void tableNdvComputedFromDataAndCapped() {
        List<String> w = new ArrayList<>();
        String req = "{\"tables\":[{\"name\":\"t\",\"columns\":[\"k\",\"v\"],\"rows\":[[1,\"a\"],[1,\"b\"],[2,\"c\"]]}]}";
        List<Table> ts = TestFramework.tables(req, w);
        Table t = ts.get(0);
        TestFramework.eq(t.rowCount, 3L, "实际行数");
        TestFramework.eq(t.ndv.get("k"), 2L, "k 的 NDV");
        TestFramework.eq(t.ndv.get("v"), 3L, "v 的 NDV");

        List<String> w2 = new ArrayList<>();
        String req2 = "{\"tables\":[{\"name\":\"t\",\"columns\":[\"k\"],\"rowCount\":5,\"ndv\":{\"k\":9}}]}";
        Table t2 = TestFramework.tables(req2, w2).get(0);
        TestFramework.eq(t2.ndv.get("k"), 5L, "NDV 超过行数应截断");
        TestFramework.check(!w2.isEmpty(), "截断应产生告警");
        TestFramework.check(t2.statsOnly, "无 rows 为仅统计表");
    }

    // ---------- 3. 估计 ----------

    /** 带指定列名/NDV 的统计表。 */
    private static Table statTable(String name, String[] cols, long rows, long... colNdv) {
        List<String> w = new ArrayList<>();
        StringBuilder sb = new StringBuilder("{\"name\":\"").append(name)
                .append("\",\"columns\":[");
        for (int i = 0; i < cols.length; i++) {
            if (i > 0) sb.append(',');
            sb.append('"').append(cols[i]).append('"');
        }
        sb.append("],\"rowCount\":").append(rows).append(",\"ndv\":{");
        for (int i = 0; i < cols.length; i++) {
            if (i > 0) sb.append(',');
            sb.append('"').append(cols[i]).append("\":").append(colNdv[i]);
        }
        sb.append("}}");
        return Table.fromJson(TestFramework.obj(sb.toString()), w);
    }

    static void cardinalityEstimationTextbookFormula() {
        // a(1000, ndv id=1000, b_id=100) ⋈ b(100, ndv id=100) => 1000
        Table a = statTable("a", new String[]{"id", "b_id"}, 1000, 1000, 100);
        Table b = statTable("b", new String[]{"id"}, 100, 100);
        List<Table> ts = Arrays.asList(a, b);
        List<JoinPred> ps = Arrays.asList(new JoinPred(0, "b_id", 1, "id"));
        Estimator est = new Estimator(ts, ps);
        Stats s = est.estimate(0b11);
        TestFramework.approx(s.rows, 1000.0, 1e-9, "两表估计行数 = |a||b|/max(100,100)");
        TestFramework.eq(s.ndv.get("a.b_id"), 100L, "结果列 NDV 不超行数");
    }

    static void estimationIsOrderIndependent() {
        // 3 表链：同一子集的估计无论怎么切分，joinRows 复算结果必须一致
        Table a = statTable("a", new String[]{"id", "b_id"}, 100000, 100000, 1000);
        Table b = statTable("b", new String[]{"id", "c_id"}, 1000, 1000, 100);
        Table c = statTable("c", new String[]{"id"}, 100, 100);
        List<Table> ts = Arrays.asList(a, b, c);
        List<JoinPred> ps = Arrays.asList(
                new JoinPred(0, "b_id", 1, "id"),
                new JoinPred(1, "c_id", 2, "id"));
        Estimator est = new Estimator(ts, ps);

        double whole = est.estimate(0b111).rows;
        // (a⋈b)⋈c
        double ab = est.estimate(0b011).rows;
        double v1 = est.joinRows(new Stats(ab, new LinkedHashMap<>()), est.estimate(0b100),
                0b011, 0b100, est.crossingPreds(0b011, 0b100));
        // (b⋈c)⋈a
        double bc = est.estimate(0b110).rows;
        double v2 = est.joinRows(new Stats(bc, new LinkedHashMap<>()), est.estimate(0b001),
                0b110, 0b001, est.crossingPreds(0b110, 0b001));
        TestFramework.approx(v1, whole, 1e-6, "(ab)c 与整体估计一致");
        TestFramework.approx(v2, whole, 1e-6, "a(bc) 与整体估计一致");
        TestFramework.approx(whole, 100000.0, 1e-6, "链三表最终估计=100000");
    }

    // ---------- 4. DP 与穷举 ----------

    static void dpPicksMinCostPlan_chain() {
        Table a = statTable("a", new String[]{"id", "b_id"}, 100000, 100000, 1000);
        Table b = statTable("b", new String[]{"id", "c_id"}, 1000, 1000, 100);
        Table c = statTable("c", new String[]{"id", "d_id"}, 100, 100, 10);
        Table d = statTable("d", new String[]{"id"}, 10, 10);
        List<Table> ts = Arrays.asList(a, b, c, d);
        List<JoinPred> ps = Arrays.asList(
                new JoinPred(0, "b_id", 1, "id"),
                new JoinPred(1, "c_id", 2, "id"),
                new JoinPred(2, "d_id", 3, "id"));
        Optimizer opt = new Optimizer(ts, ps);
        PlanNode plan = opt.bestPlan();
        // 小表一侧先连：最内层 c⋈d，再 b，再 a
        TestFramework.eq(plan.rounded(), 100000L, "根估计行数");
        TestFramework.check(plan.left.tableIdx == 0 || plan.right.tableIdx == 0
                || containsScan(plan, 0), "计划含 a 扫描");
        // 最优计划最内层必须是 c-d 连接（估计 100 行，代价最低起点）
        String canon = plan.canonical(ts);
        TestFramework.check(canon.startsWith("J"), "根为连接节点");
        TestFramework.approx(plan.cost, 203310.0, 1e-9, "总代价与手算一致");
    }

    private static boolean containsScan(PlanNode n, int t) {
        if (n.isLeaf()) return n.tableIdx == t;
        return containsScan(n.left, t) || containsScan(n.right, t);
    }

    static void dpMatchesExhaustiveEnumeration() {
        // 多张随机风格查询（链、星、环、断开），DP 最优代价必须等于穷举最小代价
        int[][][] graphCases = {
                {{0, 1}, {1, 2}, {2, 3}},                       // 链 4
                {{0, 1}, {0, 2}, {0, 3}},                       // 星 4
                {{0, 1}, {1, 2}, {2, 0}, {2, 3}},               // 环+尾 4
                {{0, 1}, {2, 3}},                               // 断开 4
                {{0, 1}, {0, 2}, {0, 3}, {0, 4}, {1, 5}},       // 6 表
                {},                                              // 全笛卡尔 4
        };
        int[] sizes = {4, 4, 4, 4, 6, 4};
        long seed = 42;
        for (int ci = 0; ci < graphCases.length; ci++) {
            int n = sizes[ci];
            List<Table> ts = new ArrayList<>();
            for (int i = 0; i < n; i++) {
                // 伪随机行数与 NDV，避免退化
                seed = (seed * 1103515245 + 12345) & 0x7fffffff;
                long rows = 10 + seed % 5000;
                long ndv = 1 + seed % rows;
                List<String> cols = new ArrayList<>();
                List<Long> nd = new ArrayList<>();
                cols.add("id"); nd.add(rows);
                for (int[] e : graphCases[ci]) {
                    if (e[0] == i || e[1] == i) {
                        int other = e[0] == i ? e[1] : e[0];
                        String col = "fk" + other;
                        cols.add(col);
                        seed = (seed * 1103515245 + 12345) & 0x7fffffff;
                        nd.add(1 + seed % Math.max(1, rows));
                    }
                }
                Table t = statTable("t" + i, cols.toArray(new String[0]),
                        rows, nd.stream().mapToLong(Long::longValue).toArray());
                ts.add(t);
            }
            List<JoinPred> ps = new ArrayList<>();
            for (int[] e : graphCases[ci]) {
                int x = e[0], y = e[1];
                String cx = "fk" + y, cy = "id";
                ps.add(new JoinPred(x, cx, y, cy));
            }
            Optimizer opt = new Optimizer(ts, ps);
            Enumerator en = new Enumerator(ts, ps);
            Enumerator.Report rep = en.fullReport();
            TestFramework.approx(opt.bestPlan().cost, rep.minCost, 1e-6,
                    "case " + ci + " DP 代价 == 穷举最小");
            TestFramework.check(rep.totalTrees == rep.expectedTreeCount,
                    "case " + ci + " 穷举树数 " + rep.totalTrees + " == 精确公式 " + rep.expectedTreeCount);
        }
    }

    static void legalTreeCountMatchesFormula() {
        // 无边 n 表：每个分量单表，合法树数 = (2n-2)!/(n-1)!，n=1..6
        long[] expected = {1, 2, 12, 120, 1680, 30240};
        for (int n = 1; n <= 6; n++) {
            List<Table> ts = new ArrayList<>();
            for (int i = 0; i < n; i++) ts.add(statTable("t" + i, new String[]{"id"}, 10, 10));
            Enumerator en = new Enumerator(ts, new ArrayList<>());
            Enumerator.Report rep = en.fullReport();
            TestFramework.eq((long) rep.totalTrees, expected[n - 1],
                    n + " 表无边时合法树数");
            TestFramework.eq(rep.expectedTreeCount, expected[n - 1],
                    n + " 表精确计数公式");
        }
        // 4 表链：只有跨相邻表的合法分区，合法树 40 棵（精确计数 DP 同样给出 40）
        List<Table> ts = new ArrayList<>();
        for (int i = 0; i < 4; i++) ts.add(statTable("t" + i, new String[]{"id"}, 10, 10));
        List<JoinPred> ps = Arrays.asList(
                new JoinPred(0, "id", 1, "id"),
                new JoinPred(1, "id", 2, "id"),
                new JoinPred(2, "id", 3, "id"));
        Enumerator.Report rep = new Enumerator(ts, ps).fullReport();
        TestFramework.eq((long) rep.totalTrees, 40L, "4 表连通链有 40 棵合法树");
        TestFramework.eq(rep.expectedTreeCount, 40L, "精确计数公式同样给出 40");

        // 4 表完全图 K4：任意分区都有跨边，合法树数达到无约束上界 f(4)=120
        List<Table> tk = new ArrayList<>();
        for (int i = 0; i < 4; i++) tk.add(statTable("u" + i, new String[]{"id"}, 10, 10));
        List<JoinPred> pk = new ArrayList<>();
        for (int i = 0; i < 4; i++)
            for (int j = i + 1; j < 4; j++)
                pk.add(new JoinPred(i, "id", j, "id"));
        Enumerator.Report repK = new Enumerator(tk, pk).fullReport();
        TestFramework.eq((long) repK.totalTrees, 120L, "K4 完全图合法树 120 棵");
    }

    // ---------- 5. 断开图与笛卡尔积 ----------

    static void disconnectedGraphMarksCartesianAndSemantics() {
        // {ab} 与 {c}：ab 各 3 行连完 3 行，再与 c(2 行) 积 = 6
        String req = "{\"tables\":["
                + "{\"name\":\"a\",\"columns\":[\"id\",\"b_id\"],\"rows\":[[1,1],[2,1],[3,2]]},"
                + "{\"name\":\"b\",\"columns\":[\"id\"],\"rows\":[[1],[2]]},"
                + "{\"name\":\"c\",\"columns\":[\"n\"],\"rows\":[[10],[20]]}"
                + "],\"joins\":[[\"a\",\"b_id\",\"b\",\"id\"]]}";
        List<String> w = new ArrayList<>();
        List<Table> ts = TestFramework.tables(req, w);
        List<JoinPred> ps = TestFramework.preds(req, ts, w);
        Optimizer opt = new Optimizer(ts, ps);
        TestFramework.eq(opt.connectedComponents().size(), 2, "两个连通分量");
        TestFramework.check(Optimizer.containsCartesian(opt.bestPlan()), "计划含笛卡尔积标记");
        Rel r = new Executor(ts).execute(opt.bestPlan());
        TestFramework.eq((long) r.rows.size(), 6L, "结果行数 3×2=6");
        // 语义与顺序无关：穷举所有合法计划，每棵树的实际结果行数都必须相同（6）
        for (PlanNode p : new Enumerator(ts, ps).enumerate(0b111)) {
            Rel rr = new Executor(ts).execute(p);
            TestFramework.eq((long) rr.rows.size(), 6L, "每种合法顺序结果均为 6 行");
        }
    }

    // ---------- 6. 执行器 ----------

    static void executorHashJoinSemantics() {
        String req = "{\"tables\":["
                + "{\"name\":\"a\",\"columns\":[\"k\",\"x\"],\"rows\":[[1,\"a\"],[2,\"b\"],[2,\"c\"]]},"
                + "{\"name\":\"b\",\"columns\":[\"k\",\"y\"],\"rows\":[[2,\"B\"],[3,\"C\"]]}"
                + "],\"joins\":[[\"a\",\"k\",\"b\",\"k\"]]}";
        List<String> w = new ArrayList<>();
        List<Table> ts = TestFramework.tables(req, w);
        List<JoinPred> ps = TestFramework.preds(req, ts, w);
        Rel r = new Executor(ts).execute(new Optimizer(ts, ps).bestPlan());
        // a 中 k=2 有两行，b 中 k=2 一行 => 2 条
        TestFramework.eq((long) r.rows.size(), 2L, "一对多哈希连接行数");
        TestFramework.eq(r.columns, Arrays.asList("a.k", "a.x", "b.k", "b.y"), "输出列顺序");
        TestFramework.eq(r.rows.get(0).get(1), "b", "连接值正确");
        TestFramework.eq(r.rows.get(1).get(1), "c", "连接值正确");
    }

    static void executorMultiPredicateJoin() {
        String req = "{\"tables\":["
                + "{\"name\":\"a\",\"columns\":[\"k1\",\"k2\"],\"rows\":[[1,9],[1,8],[2,9]]},"
                + "{\"name\":\"b\",\"columns\":[\"k1\",\"k2\"],\"rows\":[[1,9],[2,9]]}"
                + "],\"joins\":[[\"a\",\"k1\",\"b\",\"k1\"],[\"a\",\"k2\",\"b\",\"k2\"]]}";
        List<String> w = new ArrayList<>();
        List<Table> ts = TestFramework.tables(req, w);
        List<JoinPred> ps = TestFramework.preds(req, ts, w);
        Rel r = new Executor(ts).execute(new Optimizer(ts, ps).bestPlan());
        // 只有 (1,9)=(1,9) 与 (2,9)=(2,9) 两条复合键匹配
        TestFramework.eq((long) r.rows.size(), 2L, "复合键双谓词连接");
    }

    static void nullKeysDoNotMatch() {
        String req = "{\"tables\":["
                + "{\"name\":\"a\",\"columns\":[\"k\"],\"rows\":[[1],[null]]},"
                + "{\"name\":\"b\",\"columns\":[\"k\"],\"rows\":[[1],[null]]}"
                + "],\"joins\":[[\"a\",\"k\",\"b\",\"k\"]]}";
        List<String> w = new ArrayList<>();
        List<Table> ts = TestFramework.tables(req, w);
        List<JoinPred> ps = TestFramework.preds(req, ts, w);
        Rel r = new Executor(ts).execute(new Optimizer(ts, ps).bestPlan());
        TestFramework.eq((long) r.rows.size(), 1L, "NULL 不与 NULL 匹配（SQL 语义），只剩 1=1");
    }

    static void numericStringCanonicalization() {
        // 整数 1 与浮点 1.0 应匹配；字符串 "1" 不与数字 1 匹配
        String req = "{\"tables\":["
                + "{\"name\":\"a\",\"columns\":[\"k\"],\"rows\":[[1],[\"1\"]]},"
                + "{\"name\":\"b\",\"columns\":[\"k\"],\"rows\":[[1.0]]}"
                + "],\"joins\":[[\"a\",\"k\",\"b\",\"k\"]]}";
        List<String> w = new ArrayList<>();
        List<Table> ts = TestFramework.tables(req, w);
        List<JoinPred> ps = TestFramework.preds(req, ts, w);
        Rel r = new Executor(ts).execute(new Optimizer(ts, ps).bestPlan());
        TestFramework.eq((long) r.rows.size(), 1L, "1 与 1.0 匹配，字符串不匹配");
    }

    // ---------- 7. 倾斜 ----------

    static void skewedStatsEstimateVsActual() {
        // 两张 10 行表：k 分布 9 个 1 + 1 个 2；NDV=2。
        // 均匀估计 = 100/2 = 50；实际 = 9*9 + 1*1 = 82
        String req = "{\"tables\":["
                + "{\"name\":\"a\",\"columns\":[\"k\"],\"rows\":[[1],[1],[1],[1],[1],[1],[1],[1],[1],[2]]},"
                + "{\"name\":\"b\",\"columns\":[\"k\"],\"rows\":[[1],[1],[1],[1],[1],[1],[1],[1],[1],[2]]}"
                + "],\"joins\":[[\"a\",\"k\",\"b\",\"k\"]]}";
        List<String> w = new ArrayList<>();
        List<Table> ts = TestFramework.tables(req, w);
        List<JoinPred> ps = TestFramework.preds(req, ts, w);
        PlanNode plan = new Optimizer(ts, ps).bestPlan();
        TestFramework.eq(plan.rounded(), 50L, "均匀假设估计 50");
        Rel r = new Executor(ts).execute(plan);
        TestFramework.eq((long) r.rows.size(), 82L, "倾斜下实际 82，估计明显失准");
        TestFramework.check(Math.abs(plan.rounded() - plan.actualRows) == 32,
                "估计-实际差异 32");
    }

    static void nestedLoopCartesianSemantics() {
        String req = "{\"tables\":["
                + "{\"name\":\"a\",\"columns\":[\"x\"],\"rows\":[[1],[2],[3]]},"
                + "{\"name\":\"b\",\"columns\":[\"y\"],\"rows\":[[4],[5]]}"
                + "],\"joins\":[]}";
        List<String> w = new ArrayList<>();
        List<Table> ts = TestFramework.tables(req, w);
        List<JoinPred> ps = TestFramework.preds(req, ts, w);
        PlanNode plan = new Optimizer(ts, ps).bestPlan();
        TestFramework.check(plan.kind.equals(PlanNode.CARTESIAN), "无边 => 笛卡尔积节点");
        Rel r = new Executor(ts).execute(plan);
        TestFramework.eq((long) r.rows.size(), 6L, "3×2 笛卡尔积");
    }

    // ---------- 8. 端到端 ----------

    static void allLegalPlansProduceIdenticalMultiset() {
        // 含重复键、多连通分量的 4 表查询：枚举全部合法计划，
        // 每棵树执行后按“全限定列名->值”规范化，结果多重集必须逐行一致。
        String req = "{\"tables\":["
                + "{\"name\":\"a\",\"columns\":[\"id\",\"b_id\"],\"rows\":[[1,1],[2,1],[3,2],[4,2]]},"
                + "{\"name\":\"b\",\"columns\":[\"id\"],\"rows\":[[1],[2]]},"
                + "{\"name\":\"c\",\"columns\":[\"n\"],\"rows\":[[7],[8]]},"
                + "{\"name\":\"d\",\"columns\":[\"m\"],\"rows\":[[9]]}"
                + "],\"joins\":[[\"a\",\"b_id\",\"b\",\"id\"]]}";
        List<String> w = new ArrayList<>();
        List<Table> ts = TestFramework.tables(req, w);
        List<JoinPred> ps = TestFramework.preds(req, ts, w);
        Enumerator en = new Enumerator(ts, ps);
        List<PlanNode> plans = en.enumerate(0b1111);
        TestFramework.check(plans.size() >= 8, "至少有多棵合法树，实际 " + plans.size());

        java.util.Set<String> canonical = new java.util.TreeSet<>();
        int expectedRows = -1;
        for (PlanNode p : plans) {
            Rel r = new Executor(ts).execute(p);
            if (expectedRows < 0) expectedRows = r.rows.size();
            TestFramework.eq((long) r.rows.size(), (long) expectedRows, "行数一致");
            canonical.add(canonicalMultiset(r));
        }
        TestFramework.eq(canonical.size(), 1,
                "所有合法计划产生相同的结果行多重集（计划数=" + plans.size() + "）");
        TestFramework.eq((long) expectedRows, 8L, "4 条 a ⋈ 2 条 b = 4，再 ×2×1 = 8");
    }

    /** 把结果关系规范化为与列顺序、行顺序无关的多重集串。 */
    private static String canonicalMultiset(Rel r) {
        List<String> rows = new ArrayList<>();
        for (List<Object> row : r.rows) {
            List<String> kv = new ArrayList<>();
            for (int i = 0; i < r.columns.size(); i++) {
                kv.add(r.columns.get(i) + "=" + Table.canon(row.get(i)));
            }
            java.util.Collections.sort(kv);
            rows.add(String.join(",", kv));
        }
        java.util.Collections.sort(rows);
        return String.join(";", rows);
    }

    static void statsOnlySkipsExecution() throws Exception {
        String req = "{\"tables\":["
                + "{\"name\":\"a\",\"columns\":[\"id\",\"b_id\"],\"rowCount\":1000,\"ndv\":{\"id\":1000,\"b_id\":100}},"
                + "{\"name\":\"b\",\"columns\":[\"id\"],\"rowCount\":100,\"ndv\":{\"id\":100}}"
                + "],\"joins\":[[\"a\",\"b_id\",\"b\",\"id\"]]}";
        Map<String, Object> resp = new Main.Handler().handle(TestFramework.obj(req), null);
        TestFramework.check(resp.containsKey("plan"), "返回计划");
        TestFramework.check(!resp.containsKey("result"), "仅统计表不执行、无结果");
        Map<String, Object> summary = Json.asObj(resp.get("summary"));
        TestFramework.check(!summary.containsKey("actualRows"), "summary 无实际行数");
    }

    static void maxTablesLimit() throws Exception {
        boolean threw = false;
        try {
            StringBuilder sb = new StringBuilder("{\"tables\":[");
            for (int i = 0; i < 9; i++) {
                if (i > 0) sb.append(',');
                sb.append("{\"name\":\"t").append(i).append("\",\"columns\":[\"id\"],\"rowCount\":1,\"ndv\":{\"id\":1}}");
            }
            sb.append("]}");
            new Main.Handler().handle(TestFramework.obj(sb.toString()), null);
        } catch (IllegalArgumentException e) {
            threw = e.getMessage().contains("8");
        }
        TestFramework.check(threw, "9 张表应被拒绝（上限 8）");
    }

    static void endToEndHandlerAndExport() throws Exception {
        String req = "{\"tables\":["
                + "{\"name\":\"a\",\"columns\":[\"k\"],\"rows\":[[1],[2]]},"
                + "{\"name\":\"b\",\"columns\":[\"k\"],\"rows\":[[2],[3]]}"
                + "],\"joins\":[[\"a\",\"k\",\"b\",\"k\"]],\"options\":{\"enumerate\":true,\"resultLimit\":10}}";
        Path dir = Files.createTempDirectory("joinopt-export");
        Map<String, Object> resp = new Main.Handler().handle(TestFramework.obj(req), dir);
        TestFramework.eq(Json.asObj(resp.get("summary")).get("actualRows"), 1L, "实际 1 行（只有 k=2）");
        Map<String, Object> en = Json.asObj(resp.get("enumeration"));
        TestFramework.check(Boolean.TRUE.equals(en.get("matchesDp")), "穷举与 DP 一致标记");
        TestFramework.check(Files.exists(dir.resolve("plan.json")), "导出 plan.json");
        TestFramework.check(Files.exists(dir.resolve("data.json")), "导出 data.json");
        TestFramework.check(Files.exists(dir.resolve("request.json")), "导出 request.json");
        TestFramework.check(Files.exists(dir.resolve("plan.txt")), "导出 plan.txt");
        // 导出的计划可再解析
        Map<String, Object> planDoc = Json.asObj(Json.parse(Files.readString(dir.resolve("plan.json"))));
        TestFramework.check(planDoc.containsKey("plan"), "plan.json 含计划");
        // data.json 含实际数据
        Map<String, Object> dataDoc = Json.asObj(Json.parse(Files.readString(dir.resolve("data.json"))));
        TestFramework.check(dataDoc.containsKey("result"), "data.json 含结果数据");
    }

    static void badRequestsFail() throws Exception {
        expectError("{\"tables\":[]}", "空表列表");
        expectError("{\"tables\":[{\"name\":\"a\",\"columns\":[\"id\"]}]}", "仅统计表缺 rowCount");
        expectError("{\"tables\":["
                + "{\"name\":\"a\",\"columns\":[\"id\"],\"rows\":[[1]]},"
                + "{\"name\":\"a\",\"columns\":[\"id\"],\"rows\":[[1]]}]}", "表名重复");
        expectError("{\"tables\":["
                + "{\"name\":\"a\",\"columns\":[\"id\"],\"rows\":[[1]]}],"
                + "\"joins\":[[\"a\",\"id\",\"b\",\"id\"]]}", "谓词引用不存在的表");
        expectError("{\"tables\":["
                + "{\"name\":\"a\",\"columns\":[\"id\"],\"rows\":[[1]]},"
                + "{\"name\":\"b\",\"columns\":[\"id\"],\"rows\":[[1]]}],"
                + "\"joins\":[[\"a\",\"nope\",\"b\",\"id\"]]}", "谓词引用不存在的列");
        expectError("{\"tables\":["
                + "{\"name\":\"a\",\"columns\":[\"id\"],\"rows\":[[1,2]]}]}", "数据行列数不符");
    }

    private static void expectError(String req, String label) throws Exception {
        boolean threw = false;
        try {
            new Main.Handler().handle(TestFramework.obj(req), null);
        } catch (RuntimeException e) {
            threw = true;
        }
        TestFramework.check(threw, "应报错: " + label);
    }

    static void zeroRowTable() {
        String req = "{\"tables\":["
                + "{\"name\":\"a\",\"columns\":[\"k\"],\"rows\":[]},"
                + "{\"name\":\"b\",\"columns\":[\"k\"],\"rows\":[[1],[2]]}"
                + "],\"joins\":[[\"a\",\"k\",\"b\",\"k\"]]}";
        List<String> w = new ArrayList<>();
        List<Table> ts = TestFramework.tables(req, w);
        List<JoinPred> ps = TestFramework.preds(req, ts, w);
        PlanNode plan = new Optimizer(ts, ps).bestPlan();
        TestFramework.eq(plan.rounded(), 0L, "含空表时估计 0 行");
        Rel r = new Executor(ts).execute(plan);
        TestFramework.eq((long) r.rows.size(), 0L, "实际 0 行");
    }
}
