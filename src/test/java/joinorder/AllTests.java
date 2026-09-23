package joinorder;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 零依赖自动化测试。运行：java joinorder.AllTests（由 build.sh test 调用）。
 *
 * 覆盖：JSON 往返、代价模型手算、连通分量、bushy/左深 DP、
 * 小规模枚举所有合法顺序验证最小代价、笛卡尔积标记、执行器正确性、
 * 倾斜统计导致的估计/实际偏差、错误请求校验、8 表上限等。
 */
public final class AllTests {

    private static int passed = 0;
    private static int failed = 0;
    private static final List<String> failures = new ArrayList<>();

    public static void main(String[] args) {
        jsonRoundTrip();
        jsonParseErrors();
        costModelHandCalc();
        costModelMultiPredicate();
        connectedComponents();
        threeTableBushyVsLeftDeepSameChain();
        fourTableCycleHandCalc();
        bushyNeverWorseAndSometimesBetter();
        bruteForceMatchesDpAllConnectedGraphsUpTo6();
        bruteForceMatchesDpDisconnected();
        cartesianMarkedForDisconnected();
        noCartesianInsideConnectedComponent();
        leftDeepStrategy();
        executorHashJoinCorrect();
        executorCartesianCorrect();
        executorMultiPredicate();
        executorMultiTableSkew();
        skewEstimateVsActualOverestimate();
        skewEstimateVsActualUnderestimate();
        statsSourceActualRemovesBias();
        emptyTableExecutionRejected();
        nineTablesRejected();
        unknownColumnRejected();
        eightTableBoundAccepted();
        deterministicTieBreak();
        fullJsonRequestEndToEnd();

        System.out.println("----------------------------------------");
        System.out.println("通过 " + passed + " / " + (passed + failed) + "，失败 " + failed);
        if (failed > 0) {
            for (String f : failures) System.out.println("FAIL: " + f);
            System.exit(1);
        }
        System.out.println("全部测试通过。");
    }

    // ---------------------------------------------------------------- helpers

    static void check(boolean cond, String name) {
        if (cond) { passed++; }
        else { failed++; failures.add(name); System.out.println("FAIL: " + name); }
    }

    /** 允许相对误差 1e-9：多层 min/连乘的浮点求和顺序不同会产生 ~1e-10 的舍入。 */
    static void checkEq(double actual, double expected, String name) {
        double tol = Math.max(1e-6, Math.abs(expected) * 1e-9);
        if (Math.abs(actual - expected) <= tol) { passed++; }
        else {
            failed++;
            String msg = name + " (期望 " + expected + "，实际 " + actual + ")";
            failures.add(msg);
            System.out.println("FAIL: " + msg);
        }
    }

    static void throwsEngine(Runnable r, String name) {
        try {
            r.run();
            failed++;
            failures.add(name + " (期望抛出 EngineException)");
            System.out.println("FAIL: " + name + " (未抛出)");
        } catch (EngineException e) {
            passed++;
        }
    }

    /** 测试用查询构造器。 */
    static final class B {
        final Map<String, Object> req = new LinkedHashMap<>();
        final List<Object> tables = new ArrayList<>();
        final List<Object> joins = new ArrayList<>();
        int tableCount = 0;

        B(String name) {
            req.put("name", name);
            req.put("execute", false);
            req.put("verifyBruteForce", false);
            req.put("tables", tables);
            req.put("joins", joins);
        }

        B execute(boolean v) { req.put("execute", v); return this; }
        B strategy(String s) { req.put("strategy", s); return this; }
        B verify(boolean v) { req.put("verifyBruteForce", v); return this; }
        B maxTrees(long v) { req.put("bruteForceMaxTrees", v); return this; }
        B statsSource(String s) { req.put("statsSource", s); return this; }
        B preview(long v) { req.put("previewLimit", v); return this; }

        /** 仅统计（无数据）表。ndv: 偶数下标为列名、奇数为 NDV 值（缺省=行数）。 */
        B statsTable(String name, long rows, Object... ndvPairs) {
            Map<String, Object> t = new LinkedHashMap<>();
            t.put("name", name);
            t.put("columns", inferColumns(ndvPairs));
            Map<String, Object> st = new LinkedHashMap<>();
            st.put("rowCount", rows);
            Map<String, Object> ndv = new LinkedHashMap<>();
            for (int i = 0; i < ndvPairs.length; i += 2) {
                ndv.put(ndvPairs[i].toString(), ndvPairs[i + 1]);
            }
            st.put("ndv", ndv);
            t.put("stats", st);
            tables.add(t);
            tableCount++;
            return this;
        }

        private List<Object> inferColumns(Object... ndvPairs) {
            List<Object> cols = new ArrayList<>();
            for (int i = 0; i < ndvPairs.length; i += 2) {
                String c = ndvPairs[i].toString();
                if (c.indexOf('.') >= 0) c = c.substring(c.indexOf('.') + 1);
                cols.add(c);
            }
            return cols;
        }

        /** 有真实数据的表，rows 是 {"col", value, ...} 的数组。 */
        B dataTable(String name, Map<String, Object>... data) {
            Map<String, Object> t = new LinkedHashMap<>();
            t.put("name", name);
            List<Object> rs = new ArrayList<>();
            for (Map<String, Object> r : data) rs.add(r);
            t.put("rows", rs);
            tables.add(t);
            tableCount++;
            return this;
        }

        B join(String left, String right) {
            Map<String, Object> j = new LinkedHashMap<>();
            List<Object> on = new ArrayList<>();
            on.add(pred(left, right));
            j.put("on", on);
            joins.add(j);
            return this;
        }

        B joinPreds(String[]... preds) {
            Map<String, Object> j = new LinkedHashMap<>();
            List<Object> on = new ArrayList<>();
            for (String[] p : preds) on.add(pred(p[0], p[1]));
            j.put("on", on);
            joins.add(j);
            return this;
        }

        private Map<String, Object> pred(String l, String r) {
            Map<String, Object> p = new LinkedHashMap<>();
            p.put("left", l);
            p.put("right", r);
            return p;
        }

        Model model() { return Model.fromRequest(req); }

        @SuppressWarnings("unchecked")
        Map<String, Object> run() { return new Engine().run(req); }
    }

    static Map<String, Object> row(Object... kv) {
        Map<String, Object> m = new LinkedHashMap<>();
        for (int i = 0; i < kv.length; i += 2) m.put(kv[i].toString(), kv[i + 1]);
        return m;
    }

    @SuppressWarnings("unchecked")
    static Map<String, Object> obj(Object o) { return (Map<String, Object>) o; }
    @SuppressWarnings("unchecked")
    static List<Object> arr(Object o) { return (List<Object>) o; }

    static Optimizer.Result bushy(Model m, boolean provided) {
        return new Optimizer(m, new CostModel(m), provided).optimize();
    }

    // ---------------------------------------------------------------- tests

    static void jsonRoundTrip() {
        Map<String, Object> inner = new LinkedHashMap<>();
        inner.put("x", 1L);
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("a", 1L);
        m.put("b", 1.5);
        m.put("c", "hi\t\n");
        List<Object> l = new ArrayList<>();
        l.add(true); l.add(null); l.add(inner);
        m.put("d", l);
        String s = Json.write(m);
        Object back = Json.parse(s);
        check(m.equals(back), "JSON 往返保持数据与键序");
    }

    static void jsonParseErrors() {
        throwsEngine(() -> Json.parse("{bad}"), "非法 JSON 被拒绝");
        throwsEngine(() -> Json.parse("[1,2,"), "截断数组被拒绝");
        checkEq(((Number) Json.parse("3.5")).doubleValue(), 3.5, "小数解析");
        checkEq(((Number) Json.parse("42")).longValue(), 42L, "整数解析");
    }

    static void costModelHandCalc() {
        // R(a):1000,ndv=10 ; S(b):500,ndv=20 => |R⋈S| = 1000*500/20 = 25000
        Model m = new B("c").statsTable("R", 1000, "R.a", 10)
                            .statsTable("S", 500, "S.b", 20).join("R.a", "S.b").model();
        Stats l = Optimizer.baseStats(m, 0, true);
        Stats r = Optimizer.baseStats(m, 1, true);
        Stats j = new CostModel(m).estimateJoin(1, 2, l, r, m.crossingEdges(1, 2));
        checkEq(j.rowCount, 25000, "等值连接基数 |R|*|S|/max(ndv)");
        checkEq(j.ndv.get("R.a"), 10, "NDV 传播不超过原值");
        checkEq(j.ndv.get("S.b"), 20, "NDV 传播不超过原值 (2)");
        // 笛卡尔积
        Stats x = new CostModel(m).estimateJoin(1, 2, l, r, new ArrayList<>());
        checkEq(x.rowCount, 500000, "无边 => 笛卡尔积行数");
    }

    static void costModelMultiPredicate() {
        // 两条件：R.a=S.a (ndv 10) 且 R.k=S.k (ndv 5) => 行数 / (10*5)
        B b = new B("mp")
                .statsTable("R", 1000, "R.a", 10, "R.k", 5)
                .statsTable("S", 2000, "S.a", 8, "S.k", 5);
        b.joinPreds(new String[]{"R.a", "S.a"}, new String[]{"R.k", "S.k"});
        Model m = b.model();
        Stats l = Optimizer.baseStats(m, 0, true);
        Stats r = Optimizer.baseStats(m, 1, true);
        Stats j = new CostModel(m).estimateJoin(1, 2, l, r, m.crossingEdges(1, 2));
        // max(10,8)=10, max(5,5)=5
        checkEq(j.rowCount, 1000.0 * 2000 / 50, "多等值条件选择率连乘");
    }

    static void connectedComponents() {
        // a-b 连通，c-d 连通，e 孤立 => 3 个分量
        Model m = new B("cc").statsTable("a", 10, "a.k", 10)
                             .statsTable("b", 10, "b.k", 10)
                             .statsTable("c", 10, "c.k", 10)
                             .statsTable("d", 10, "d.k", 10)
                             .statsTable("e", 10, "e.k", 10)
                             .join("a.k", "b.k").join("c.k", "d.k").model();
        checkEq(m.components().size(), 3, "检测到 3 个连通分量");
        check(m.isConnected(3), "a,b 连通");
        check(!m.isConnected(5), "a,c 不连通");
    }

    static void threeTableBushyVsLeftDeepSameChain() {
        // 链 R-S-T，均匀 NDV：验证两种策略都可得到、且稠密不劣于左深
        B b = new B("chain3")
                .statsTable("R", 100, "R.k", 100)
                .statsTable("S", 100, "S.rk", 100, "S.tk", 100)
                .statsTable("T", 100, "T.k", 100)
                .join("R.k", "S.rk").join("S.tk", "T.k");
        Model m = b.model();
        double bc = bushy(m, true).plan.estimatedCost;
        double lc = new LeftDeepOptimizer(m, new CostModel(m), true).optimize().plan.estimatedCost;
        check(bc <= lc + 1e-9, "链图：稠密代价 <= 左深代价");
        // 手算：每步 100*100/100=100；最优 ((R⋈S)⋈T) 或 (R⋈(S⋈T))：scan 300 + join100 + join100 = 500
        checkEq(bc, 500, "链图最优代价手算 500");
    }

    static void fourTableCycleHandCalc() {
        // 4 表环 a-b-c-d-a，均匀统计：稠密与左深都能达到同一最优值 4300。
        // （此例展示稠密计划如何把两对小表先连接；对称统计下左深也能打平。
        //   稠密严格优于左深的非对称场景见 bushyNeverWorseAndSometimesBetter。）
        B b = new B("cycle4")
                .statsTable("a", 100, "a.bk", 100, "a.dk", 100)
                .statsTable("b", 1000, "b.ak", 100, "b.ck", 100)
                .statsTable("c", 100, "c.bk", 100, "c.dk", 100)
                .statsTable("d", 1000, "d.ak", 100, "d.ck", 100)
                .join("a.bk", "b.ak").join("b.ck", "c.bk")
                .join("c.dk", "d.ck").join("d.ak", "a.dk");
        Model m = b.model();
        Optimizer.Result or = bushy(m, true);
        LeftDeepOptimizer.Result ld = new LeftDeepOptimizer(m, new CostModel(m), true).optimize();
        // (a⋈b)=1000、(c⋈d)=1000，再经 b-c、d-a 两边：1000*1000/(100*100)=100
        // 稠密总代价 = 2100 + 2100 + 100 = 4300
        checkEq(or.plan.estimatedCost, 4300, "4 表环稠密最优代价 4300");
        checkEq(ld.plan.estimatedCost, 4300, "4 表环最优左深代价同为 4300（对称统计打平）");
    }

    static void bushyNeverWorseAndSometimesBetter() {
        // 随机搜索 n=4..6 的非对称统计：稠密代价永远不高于左深；且必须存在严格更优的实例。
        double bestGap = 0;
        String bestShape = "";
        for (int n = 4; n <= 6; n++) {
            for (long seed = 1; seed <= 400; seed++) {
                B b = new B("shape-" + n + "-" + seed);
                java.util.Random rnd = new java.util.Random(seed * 31 + n);
                for (int i = 0; i < n; i++) {
                    long rows = 1 + rnd.nextInt(5000);
                    Object[] ndv = new Object[2 * n];
                    for (int j = 0; j < n; j++) {
                        ndv[2 * j] = "t" + i + ".c" + j;
                        ndv[2 * j + 1] = 1 + rnd.nextInt((int) Math.max(1, rows));
                    }
                    b.statsTable("t" + i, rows, ndv);
                }
                // 随机连通图（生成树 + 随机边）
                List<Integer> in = new ArrayList<>();
                in.add(0);
                List<Integer> todo = new ArrayList<>();
                for (int i = 1; i < n; i++) todo.add(i);
                java.util.Collections.shuffle(todo, rnd);
                for (int v : todo) {
                    int u = in.get(rnd.nextInt(in.size()));
                    b.join("t" + u + ".c" + v, "t" + v + ".c" + u);
                    in.add(v);
                }
                int extra = rnd.nextInt((n * (n - 1)) / 2 - (n - 1) + 1);
                for (int e = 0; e < extra; e++) {
                    int u = rnd.nextInt(n), v = rnd.nextInt(n);
                    if (u == v) continue;
                    b.join("t" + u + ".c" + v, "t" + v + ".c" + u); // 同表对重复谓词会被合并为多条件
                }
                Model m = b.model();
                double bc = bushy(m, true).plan.estimatedCost;
                double lc = new LeftDeepOptimizer(m, new CostModel(m), true).optimize().plan.estimatedCost;
                check(bc <= lc + 1e-6, "n=" + n + " seed=" + seed + " 稠密不劣于左深");
                if (lc - bc > bestGap + 1e-6) {
                    bestGap = lc - bc;
                    bestShape = "n=" + n + " seed=" + seed;
                }
            }
        }
        check(bestGap > 1e-6, "存在稠密严格优于左深的非对称实例（" + bestShape
                + "，左深-稠密=" + PlanNode.round(bestGap) + "）");
        System.out.println("  [信息] 稠密严格优于左深的最大代价差: "
                + PlanNode.round(bestGap) + " (" + bestShape + ")");
    }

    static void bruteForceMatchesDpAllConnectedGraphsUpTo6() {
        // 固定 n=6，枚举 200 个随机连通图（生成树 + 随机边），列与 NDV 随机：
        // 暴力全空间（允许任意分割/笛卡尔积）最小代价必须等于稠密 DP。
        int n = 6;
        int graphs = 0;
        for (long seed = 1; seed <= 200; seed++) {
            B b = new B("rand" + seed);
            java.util.Random rnd = new java.util.Random(seed);
            long[] rows = new long[n];
            for (int i = 0; i < n; i++) {
                rows[i] = 1 + rnd.nextInt(1000);
                // 每张表声明 n 个“树边列”c_j 与 n 个“随边列”q_j
                Object[] ndv = new Object[4 * n];
                for (int j = 0; j < n; j++) {
                    ndv[2 * j] = "t" + i + ".c" + j;
                    ndv[2 * j + 1] = 1 + rnd.nextInt((int) rows[i]);
                    ndv[2 * (n + j)] = "t" + i + ".q" + j;
                    ndv[2 * (n + j) + 1] = 1 + rnd.nextInt((int) rows[i]);
                }
                b.statsTable("t" + i, rows[i], ndv);
            }
            // 保证连通：随机生成树
            List<Integer> in = new ArrayList<>();
            in.add(0);
            List<Integer> todo = new ArrayList<>();
            for (int i = 1; i < n; i++) todo.add(i);
            java.util.Collections.shuffle(todo, rnd);
            for (int v : todo) {
                int u = in.get(rnd.nextInt(in.size()));
                b.join("t" + u + ".c" + v, "t" + v + ".c" + u);
                in.add(v);
            }
            int extra = rnd.nextInt(5);
            for (int e = 0; e < extra; e++) {
                int u = rnd.nextInt(n), v2 = rnd.nextInt(n);
                if (u == v2) continue;
                b.join("t" + u + ".q" + v2, "t" + v2 + ".q" + u);
            }
            Model m = b.model();
            Optimizer.Result or = bushy(m, true);
            BruteForce.Report rep = new BruteForce(m, new CostModel(m), true, 5_000_000).enumerate();
            check(rep.run, "随机图 #" + seed + " 暴力枚举在限额内完成");
            checkEq(rep.minCost, or.plan.estimatedCost,
                    "随机图 #" + seed + " DP 代价 == 全空间枚举最小值");
            graphs++;
        }
        check(graphs == 200, "随机图测试全部执行 (200 个)");
    }

    static void bruteForceMatchesDpDisconnected() {
        // 3 分量：a⋈b 相连，c、d 各自孤立（4 表）。
        // 统计：a:100 ndv50, b:200 ndv50 => a⋈b=400；c:300；d:400。
        // 全空间枚举（含中间笛卡尔积）决定合并顺序与形状；DP 必须与其一致。
        B b = new B("disc-bf")
                .statsTable("a", 100, "a.k", 50)
                .statsTable("b", 200, "b.k", 50)
                .statsTable("c", 300)
                .statsTable("d", 400)
                .join("a.k", "b.k");
        Model m = b.model();
        Optimizer.Result or = bushy(m, true);
        BruteForce.Report rep = new BruteForce(m, new CostModel(m), true, 5_000_000).enumerate();
        checkEq(rep.minCost, or.plan.estimatedCost, "断开图：枚举最小值 == DP（含分量合并顺序）");
        // 顶层最终结果必然是 400*300*400 = 48,000,000 行
        checkEq(or.plan.estimated.rowCount, 48_000_000, "断开图最终行数 48,000,000");
        // 3 个分量 => 计划中至少有 2 个跨分量笛卡尔积节点
        check(or.cartesianJoins >= 2, "断开 3 分量计划至少含 2 个跨分量笛卡尔积节点");
    }

    static void cartesianMarkedForDisconnected() {
        B b = new B("cart");
        b.dataTable("a", row("k", 1), row("k", 2), row("k", 3));
        b.dataTable("b", row("k", 1), row("k", 2), row("k", 3), row("k", 4));
        b.dataTable("c", row("k", 1), row("k", 2), row("k", 3),
                    row("k", 4), row("k", 5));
        b.join("a.k", "b.k").execute(true).preview(0);
        Map<String, Object> resp = b.run();
        // 找到标记
        int[] marked = {0};
        countCart(resp.get("plan"), marked);
        check(marked[0] >= 1, "断开分量间的连接节点被标记 cartesian=true");
        List<?> w = (List<?>) resp.get("warnings");
        check(w != null && w.toString().contains("连通分量"), "断开连接图产生中文告警");
        // a⋈b 实际 3 行（键 1,2,3 命中），再 ×c 5 行 = 15 行
        Map<?, ?> ex = obj(resp.get("execution"));
        checkEq(((Number) ex.get("resultRowCount")).longValue(), 15L, "等值连接后笛卡尔积实际行数 3*5=15");
    }

    private static void countCart(Object node, int[] marked) {
        if (!(node instanceof Map)) return;
        Map<?, ?> mp = (Map<?, ?>) node;
        if (Boolean.TRUE.equals(mp.get("cartesian"))) marked[0]++;
        countCart(mp.get("left"), marked);
        countCart(mp.get("right"), marked);
    }

    static void noCartesianInsideConnectedComponent() {
        // 连通 3 表小例：验证若计划含笛卡尔积节点，该节点的分割必然不跨越任何连接边
        // （即不会丢失等值谓词）。此统计下最优计划就是连通计划（0 个笛卡尔积）。
        B b = new B("nocart");
        b.dataTable("a", row("k", 1, "j", 1), row("k", 2, "j", 1),
                          row("k", 1, "j", 2), row("k", 2, "j", 2));
        b.dataTable("b", row("k", 1), row("k", 2));
        b.dataTable("c", row("j", 1), row("j", 2));
        b.join("a.k", "b.k").join("a.j", "c.j").execute(true).preview(100);
        Map<String, Object> resp = b.run();
        int[] marked = {0};
        countCart(resp.get("plan"), marked);
        checkEq(marked[0], 0, "连通 3 表在该统计下最优计划不含笛卡尔积节点");
        // a⋈b(k) 输出 4 行；再 ⋈c(a.j=c.j)：a 中 j 取值 1,2 各 2 行，各匹配 1 个 c => 4 行
        Map<?, ?> ex = obj(resp.get("execution"));
        checkEq(((Number) ex.get("resultRowCount")).longValue(), 4L, "无丢谓词：实际结果 4 行");
    }

    static void leftDeepStrategy() {
        B b = new B("ld").statsTable("a", 10, "a.k", 10)
                         .statsTable("b", 10, "b.k", 10)
                         .statsTable("c", 10, "c.k", 10)
                         .join("a.k", "b.k").join("b.k", "c.k");
        b.strategy("left-deep");
        Map<String, Object> resp = b.run();
        check("left-deep".equals(resp.get("strategy")), "strategy=left-deep 生效");
        // 左深计划的根右孩子必须是 scan
        Map<?, ?> plan = obj(resp.get("plan"));
        checkEq(nodeKind((Map<?, ?>) plan.get("right")), 1, "左深根节点右侧是扫描");
    }

    private static int nodeKind(Map<?, ?> node) {
        return "scan".equals(node.get("type")) ? 1 : 0;
    }

    static void executorHashJoinCorrect() {
        B b = new B("hj");
        b.dataTable("R", row("id", 1, "x", "p"), row("id", 2, "x", "q"), row("id", 2, "x", "r"));
        b.dataTable("S", row("rid", 2, "y", "s"), row("rid", 3, "y", "t"));
        b.join("R.id", "S.rid").execute(true).preview(100);
        Map<String, Object> resp = b.run();
        Map<?, ?> ex = obj(resp.get("execution"));
        checkEq(((Number) ex.get("resultRowCount")).longValue(), 2L, "HashJoin 实际输出 2 行（id=2 一对多）");
        // 估计（无提供 stats，按真实 NDV：R.id ndv=2, S.rid ndv=2 => 3*2/2=3；min 1 约定不影响）
        Map<?, ?> root = obj(ex.get("root"));
        checkEq(((Number) root.get("estimatedRows")).doubleValue(), 3.0, "估计 3 行 vs 实际 2 行");
    }

    static void executorCartesianCorrect() {
        B b = new B("xc");
        b.dataTable("a", row("v", 1), row("v", 2));
        b.dataTable("b", row("w", 10), row("w", 20), row("w", 30));
        b.execute(true).preview(100);
        Map<String, Object> resp = b.run();
        Map<?, ?> ex = obj(resp.get("execution"));
        checkEq(((Number) ex.get("resultRowCount")).longValue(), 6L, "笛卡尔积实际输出 2*3=6 行");
    }

    static void executorMultiPredicate() {
        B b = new B("mpe");
        b.dataTable("R", row("a", 1, "k", "x"), row("a", 1, "k", "y"));
        b.dataTable("S", row("a2", 1, "k2", "x"), row("a2", 1, "k2", "z"));
        b.joinPreds(new String[]{"R.a", "S.a2"}, new String[]{"R.k", "S.k2"});
        b.execute(true).preview(100);
        Map<String, Object> resp = b.run();
        Map<?, ?> ex = obj(resp.get("execution"));
        checkEq(((Number) ex.get("resultRowCount")).longValue(), 1L, "组合键 HashJoin 仅 1 行匹配");
    }

    static void executorMultiTableSkew() {
        // 3 表连接执行结果与朴素嵌套循环语义一致（抽样手工校验最终行数）
        B b = new B("mts");
        b.dataTable("o", row("id", 1), row("id", 2), row("id", 2));
        b.dataTable("l", row("oid", 2, "pid", 9), row("oid", 2, "pid", 9));
        b.dataTable("p", row("id", 9), row("id", 7));
        b.join("o.id", "l.oid").join("l.pid", "p.id");
        b.execute(true).preview(1000);
        Map<String, Object> resp = b.run();
        Map<?, ?> ex = obj(resp.get("execution"));
        checkEq(((Number) ex.get("resultRowCount")).longValue(), 4L, "三表连接实际 4 行（2 订单 × 2 明细，均命中 p9）");
    }

    static void skewEstimateVsActualOverestimate() {
        // 倾斜（高估）：orders 1000 行 cust_id NDV=100；customers 100 行 id NDV=100
        // 估计 = 1000*100/100 = 1000；实际数据所有订单都指向同一个 cust（且存在），
        // 真实输出 = 1000 行 —— 用“键不相交”构造高估：订单键都在 900..，客户键在 0..
        B b = new B("skew-over");
        Map<String, Object>[] orders = skewRows("cid", 100, i -> 900 + (i % 100)); // 100 个不同键
        b.dataTable("orders", orders);
        Map<String, Object>[] custs = skewRows("id", 100, i -> i); // 键 0..99，不相交
        b.dataTable("customers", custs);
        attachStats(b, 0, 100, row("orders.cid", 100));
        attachStats(b, 1, 100, row("customers.id", 100));
        b.join("orders.cid", "customers.id").execute(true).preview(0);
        Map<String, Object> resp = b.run();
        Map<?, ?> ex = obj(resp.get("execution"));
        Map<?, ?> root = obj(ex.get("root"));
        checkEq(((Number) root.get("estimatedRows")).doubleValue(), 100.0, "高估场景：估计 100 行");
        checkEq(((Number) root.get("actualRows")).longValue(), 0L, "高估场景：实际 0 行（键不相交）");
        check(resp.get("warnings") != null && resp.get("warnings").toString().contains("提供的统计"),
                "提示优化器使用了提供统计");
    }

    static void skewEstimateVsActualUnderestimate() {
        // 倾斜（低估）：两边 NDV 声称很大 => 估计很小；实际键高度集中 => 输出爆炸
        B b = new B("skew-under");
        // orders 100 行全部 cid=1；customers 100 行全部 id=1 => 实际 10000
        b.dataTable("orders", skewRows("cid", 100, i -> 1));
        b.dataTable("customers", skewRows("id", 100, i -> 1));
        attachStats(b, 0, 100, row("cid", 100));
        attachStats(b, 1, 100, row("id", 100));
        b.join("orders.cid", "customers.id").execute(true).preview(0);
        Map<String, Object> resp = b.run();
        Map<?, ?> root = obj(obj(resp.get("execution")).get("root"));
        checkEq(((Number) root.get("estimatedRows")).doubleValue(), 100.0, "低估场景：估计 100 行");
        checkEq(((Number) root.get("actualRows")).longValue(), 10000L, "低估场景：实际 10000 行");
        checkEq(((Number) root.get("rowRatio")).doubleValue(), 0.01, "估计/实际 = 0.01");
    }

    static void statsSourceActualRemovesBias() {
        // 与低估场景相同数据，但 statsSource=actual：优化器看到真实 NDV=1 => 估计 = 100*100/1 = 10000
        B b = new B("actual-src");
        b.dataTable("orders", skewRows("cid", 100, i -> 1));
        b.dataTable("customers", skewRows("id", 100, i -> 1));
        attachStats(b, 0, 100, row("cid", 100));
        attachStats(b, 1, 100, row("id", 100));
        b.join("orders.cid", "customers.id").execute(true).preview(0).statsSource("actual");
        Map<String, Object> resp = b.run();
        Map<?, ?> root = obj(obj(resp.get("execution")).get("root"));
        checkEq(((Number) root.get("estimatedRows")).doubleValue(), 10000.0,
                "statsSource=actual 时估计与实际一致 10000");
    }

    @SuppressWarnings("unchecked")
    private static void attachStats(B b, int tableIdx, long rowCount, Map<String, Object> ndv) {
        Map<String, Object> t = obj(b.tables.get(tableIdx));
        Map<String, Object> st = new LinkedHashMap<>();
        st.put("rowCount", rowCount);
        st.put("ndv", ndv);
        t.put("stats", st);
    }

    interface IntFn { int apply(int i); }

    @SuppressWarnings("unchecked")
    private static Map<String, Object>[] skewRows(String col, int n, IntFn key) {
        Map<String, Object>[] rs = new Map[n];
        for (int i = 0; i < n; i++) rs[i] = row(col, key.apply(i), "pad", i);
        return rs;
    }

    static void emptyTableExecutionRejected() {
        B b = new B("empty").statsTable("a", 10, "a.k", 10)
                             .statsTable("b", 10, "b.k", 10)
                             .join("a.k", "b.k").execute(true);
        throwsEngine(b::run, "无数据表在 execute=true 时被拒绝");
    }

    static void nineTablesRejected() {
        B b = new B("nine");
        for (int i = 0; i < 9; i++) b.statsTable("t" + i, 1, "t" + i + ".k", 1);
        for (int i = 1; i < 9; i++) b.join("t0.k", "t" + i + ".k");
        throwsEngine(b::model, "超过 8 张表被拒绝");
    }

    static void unknownColumnRejected() {
        B b = new B("uc").statsTable("a", 3, "a.k", 3).statsTable("b", 3, "b.k", 3);
        b.join("a.k", "b.missing");
        throwsEngine(b::model, "引用不存在列被拒绝");
        throwsEngine(() -> new B("s").statsTable("a", 3, "a.k", 3).join("a.k", "x.k").model(),
                "引用不存在表被拒绝");
    }

    static void eightTableBoundAccepted() {
        B b = new B("eight");
        for (int i = 0; i < 8; i++) b.statsTable("t" + i, 10, "t" + i + ".k", 10);
        for (int i = 1; i < 8; i++) b.join("t0.k", "t" + i + ".k");
        b.verify(true).maxTrees(50_000_000L);
        Map<String, Object> resp = b.run();
        checkEq(((Number) resp.get("tableCount")).longValue(), 8L, "8 表请求被接受");
        Map<?, ?> vf = obj(resp.get("bruteForceVerification"));
        check(Boolean.TRUE.equals(vf.get("performed")), "8 表暴力枚举执行（星形图计划数较小）");
        check(Boolean.TRUE.equals(vf.get("matchesMinimum")), "8 表：DP 仍是全空间最优");
    }

    static void deterministicTieBreak() {
        // 完全对称的两表/三表计划，重复运行文本必须一致
        B b1 = new B("det").statsTable("a", 10, "a.k", 10)
                            .statsTable("b", 10, "b.k", 10)
                            .statsTable("c", 10, "c.k", 10)
                            .join("a.k", "b.k").join("b.k", "c.k");
        String t1 = (String) b1.run().get("planText");
        String t2 = (String) new B("det2").statsTable("a", 10, "a.k", 10)
                                          .statsTable("b", 10, "b.k", 10)
                                          .statsTable("c", 10, "c.k", 10)
                                          .join("a.k", "b.k").join("b.k", "c.k")
                                          .run().get("planText");
        check(t1.equals(t2.replace("det2", "det")), "对称统计下计划确定可复现");
    }

    static void fullJsonRequestEndToEnd() {
        // 从 JSON 文本进入，到 JSON 响应输出，全链路
        String req = "{\"name\":\"e2e\",\"execute\":true,\"previewLimit\":5,"
                + "\"tables\":[{\"name\":\"r\",\"rows\":[{\"k\":1},{\"k\":1}]},"
                + "{\"name\":\"s\",\"rows\":[{\"k\":1},{\"k\":2}]}],"
                + "\"joins\":[{\"on\":[{\"left\":\"r.k\",\"right\":\"s.k\"}]}]}";
        Map<String, Object> resp = new Engine().run(Json.asObject(Json.parse(req), "req"));
        String text = Json.write(resp);
        check(text.contains("hash-join"), "端到端响应含 hash-join");
        checkEq(((Number) obj(obj(resp.get("execution")).get("root")).get("actualRows")).longValue(),
                2L, "端到端实际行数 2");
    }
}
