package ppd.tests;

import ppd.*;

import java.util.ArrayList;
import java.util.List;

/**
 * 验收核心：小表穷举比较改写前/后结果。
 *
 * 两张 2 行小表，每个单元格取 {NULL, 1, 2} => 每张表 3^4=81 种取值，
 * 在所有数据库上执行多类谓词 × 内/左连接 × 投影，逐行（包语义）比较改写前后结果。
 * 重点覆盖：右表 NULL 过滤、重名列、常量 FALSE 谓词、投影穿透/阻断。
 */
public class ExhaustiveTest {

    private static final Object[] DOMAIN = {null, 1L, 2L};

    public static void run(Assert a) {
        a.section("穷举：生成小表数据库（3^4 × 3^4 = 6561 个库）");
        List<Table> ls = databases("l");
        List<Table> rs = databases("r");
        a.check("左表枚举 81 种", ls.size() == 81);
        a.check("右表枚举 81 种", rs.size() == 81);

        String[] leftPredicates = {
                "l.a = 1",                       // 保留侧：下推左
                "r.b = 1",                       // NULL 补充侧拒绝 NULL：降级+下推
                "r.b IS NULL",                   // 非拒绝：必须保留在上方
                "r.b IS NOT NULL",               // 拒绝：降级+下推
                "(r.b = 1) OR (r.b IS NULL)",    // 保留行可满足：必须留在上方
                "FALSE",                         // 常量假：只能落保留侧
                "l.a = r.b",                     // 跨两侧且拒绝右 NULL：降级并入 ON
                "(l.a = 1) AND (r.b IS NULL)",   // 混合：左侧下推 + 右侧留上
                "(r.b = 1) OR (l.a = 2)",        // 跨两侧 OR；右 NULL 时可能由左列为真
                "l.a IS NOT NULL AND r.b = 1",   // 合取：左拒绝 + 右拒绝 => 降级
                "r.b + 1 > 1",                   // 算术比较，右 NULL => UNKNOWN：拒绝
                "NOT (r.b = 1)",                 // UNKNOWN 取反仍 UNKNOWN：拒绝
        };

        String[] innerPredicates = {
                "l.a = 1", "r.b = 1", "l.a = r.b",
                "FALSE", "(l.a = 1) AND (r.b = 2)", "r.b IS NULL",
        };

        long comparisons = 0;
        long mismatches = 0;
        List<String> mismatchDetails = new ArrayList<>();

        for (Table l : ls) {
            for (Table r : rs) {
                Plan.Scan sl = new Plan.Scan("l", l);
                Plan.Scan sr = new Plan.Scan("r", r);

                for (String p : leftPredicates) {
                    Plan lj = new Plan.LeftJoin(sl, sr,
                            List.of(ExprParser.parse("l.id = r.id")));
                    comparisons++;
                    if (!compareRewrite(a, "LJ " + p, new Plan.Filter(ExprParser.parse(p), lj),
                            false, mismatchDetails)) {
                        mismatches++;
                    }
                }
                for (String p : innerPredicates) {
                    Plan ij = new Plan.InnerJoin(sl, sr,
                            List.of(ExprParser.parse("l.id = r.id")));
                    comparisons++;
                    if (!compareRewrite(a, "IJ " + p, new Plan.Filter(ExprParser.parse(p), ij),
                            false, mismatchDetails)) {
                        mismatches++;
                    }
                }

                // 投影在中间：简单引用 => 应穿透
                Plan projected = new Plan.Project(
                        List.of(new Expr.Ref("l", "id"), new Expr.Ref("l", "a"),
                                new Expr.Ref("r", "id"), new Expr.Ref("r", "b")),
                        new Plan.LeftJoin(sl, sr,
                                List.of(ExprParser.parse("l.id = r.id"))));
                comparisons++;
                if (!compareRewrite(a, "LJ+Project r.b=1",
                        new Plan.Filter(ExprParser.parse("r.b = 1"), projected),
                        false, mismatchDetails)) {
                    mismatches++;
                }
                comparisons++;
                if (!compareRewrite(a, "LJ+Project r.b IS NULL",
                        new Plan.Filter(ExprParser.parse("r.b IS NULL"), projected),
                        false, mismatchDetails)) {
                    mismatches++;
                }

                // 投影含计算列：谓词引用计算列 => 阻断；引用来源列 => 穿透
                Plan computed = new Plan.Project(
                        List.of(new Expr.Ref("l", "id"), ExprParser.parse("l.a + r.b")),
                        new Plan.InnerJoin(sl, sr,
                                List.of(ExprParser.parse("l.id = r.id"))));
                comparisons++;
                if (!compareRewrite(a, "IJ+ComputedProj expr",
                        new Plan.Filter(ExprParser.parse("expr > 0"), computed),
                        false, mismatchDetails)) {
                    mismatches++;
                }
            }
        }

        System.out.println("  共比较 " + comparisons + " 个 (数据库 × 查询) 组合，不一致 " + mismatches + " 个");
        a.check("穷举比较总数 = 6561 × 21 = 137781", comparisons == 6561L * 21L);
        a.check("穷举：改写前后结果全部一致", mismatches == 0);
        for (String d : mismatchDetails) a.fail("穷举不一致", d);

        structuralAssertions(a, ls.get(0), rs.get(0));
        handComputedAssertions(a);
        duplicateColumnAssertions(a);
    }

    /** 执行改写前/后并比较；requireChanged 控制是否要求计划确实变化。 */
    static boolean compareRewrite(Assert a, String label, Plan original,
                                  boolean requireChanged, List<String> details) {
        try {
            PushdownRewriter.RewriteResult rr = new PushdownRewriter().rewrite(original);
            Executor exec = new Executor();
            List<Row> before = exec.execute(original);
            List<Row> after = exec.execute(rr.plan());
            boolean eq = QueryEngine.bagEquals(before, after);
            if (!eq) {
                details.add(label + " before=" + before + " after=" + after);
                return false;
            }
            if (requireChanged && rr.plan().equals(original)) {
                details.add(label + " 计划未发生改写");
                return false;
            }
            return true;
        } catch (RuntimeException ex) {
            details.add(label + " 抛异常: " + ex);
            return false;
        }
    }

    // ------------------------------------------------------------------
    // 结构性断言：决策动作符合预期（不只是结果等价）
    // ------------------------------------------------------------------

    private static void structuralAssertions(Assert a, Table l, Table r) {
        a.section("穷举：改写动作的结构性断言");

        PushdownRewriter rw = new PushdownRewriter();

        // 1. r.b IS NULL 必须留在左外连接上方
        Plan p1 = new Plan.Filter(ExprParser.parse("r.b IS NULL"),
                new Plan.LeftJoin(new Plan.Scan("l", l), new Plan.Scan("r", r),
                        List.of(ExprParser.parse("l.id = r.id"))));
        PushdownRewriter.RewriteResult rr1 = rw.rewrite(p1);
        a.check("r.b IS NULL：Filter 仍在 LeftJoin 上方",
                rr1.plan() instanceof Plan.Filter f && f.child() instanceof Plan.LeftJoin);
        a.check("决策含 STAY_ABOVE_OUTER_JOIN",
                rr1.decisions().stream().anyMatch(d -> d.action().equals("STAY_ABOVE_OUTER_JOIN")));

        // 2. r.b = 1：降级为内连接并下推右表
        PushdownRewriter.RewriteResult rr2 = rw.rewrite(
                new Plan.Filter(ExprParser.parse("r.b = 1"),
                        new Plan.LeftJoin(new Plan.Scan("l", l), new Plan.Scan("r", r),
                                List.of(ExprParser.parse("l.id = r.id")))));
        boolean downgraded = containsNode(rr2.plan(), Plan.InnerJoin.class)
                && !containsNode(rr2.plan(), Plan.LeftJoin.class);
        a.check("r.b = 1：LeftJoin 被降级为 InnerJoin", downgraded);
        a.check("r.b = 1：右表下推为 Filter->Scan(r)",
                filterOnScan(rr2.plan(), "r"));

        // 3. l.a = 1：保留侧下推，LeftJoin 保持不变
        PushdownRewriter.RewriteResult rr3 = rw.rewrite(
                new Plan.Filter(ExprParser.parse("l.a = 1"),
                        new Plan.LeftJoin(new Plan.Scan("l", l), new Plan.Scan("r", r),
                                List.of(ExprParser.parse("l.id = r.id")))));
        a.check("l.a = 1：保留 LeftJoin", containsNode(rr3.plan(), Plan.LeftJoin.class));
        a.check("l.a = 1：左表下推为 Filter->Scan(l)",
                filterOnScan(rr3.plan(), "l"));

        // 4. FALSE：落保留侧，LeftJoin 结构保留
        PushdownRewriter.RewriteResult rr4 = rw.rewrite(
                new Plan.Filter(new Expr.Lit(Boolean.FALSE),
                        new Plan.LeftJoin(new Plan.Scan("l", l), new Plan.Scan("r", r),
                                List.of(ExprParser.parse("l.id = r.id")))));
        a.check("FALSE：谓词落到左子树（保留侧）",
                filterOnScan(rr4.plan(), "l")
                        && containsNode(rr4.plan(), Plan.LeftJoin.class));

        // 5. 内连接跨侧 => 并入 ON
        PushdownRewriter.RewriteResult rr5 = rw.rewrite(
                new Plan.Filter(ExprParser.parse("l.a = r.b"),
                        new Plan.InnerJoin(new Plan.Scan("l", l), new Plan.Scan("r", r),
                                List.of(ExprParser.parse("l.id = r.id")))));
        boolean merged = rr5.plan() instanceof Plan.InnerJoin ij
                && ij.onPredicates().size() == 2;
        a.check("内连接跨侧谓词并入 ON", merged);

        // 6. 计算列阻断投影穿透
        Plan p6 = new Plan.Filter(ExprParser.parse("expr > 0"),
                new Plan.Project(
                        List.of(new Expr.Ref("l", "id"), ExprParser.parse("l.a + r.b")),
                        new Plan.InnerJoin(new Plan.Scan("l", l), new Plan.Scan("r", r),
                                List.of(ExprParser.parse("l.id = r.id")))));
        PushdownRewriter.RewriteResult rr6 = rw.rewrite(p6);
        a.check("计算列谓词保留在 Project 上方",
                rr6.plan() instanceof Plan.Filter f && f.child() instanceof Plan.Project);
    }

    // ------------------------------------------------------------------
    // 手工小例：直接验证 SQL 真值（不依赖重写器）
    // ------------------------------------------------------------------

    private static void handComputedAssertions(Assert a) {
        a.section("穷举：手工真值用例（WHERE r.b IS NULL 的保留行语义）");
        Table l = t("l", List.of("id", "a"),
                new Object[]{1L, 10L}, new Object[]{2L, 20L}, new Object[]{3L, 30L});
        // r.id=1 的 w 为 NULL；没有 id=3 的右行
        Table r = t("r", List.of("id", "b"),
                new Object[]{1L, 100L}, new Object[]{2L, null});
        Plan lj = new Plan.LeftJoin(new Plan.Scan("l", l), new Plan.Scan("r", r),
                List.of(ExprParser.parse("l.id = r.id")));

        List<Row> isnull = new Executor().execute(
                new Plan.Filter(ExprParser.parse("r.b IS NULL"), lj));
        a.check("r.b IS NULL 命中 2 行（匹配上的 NULL 值 + 未匹配保留行）",
                isnull.size() == 2);
        a.check("第 1 行是 l.id=2（右值本身为 NULL）", isnull.get(0).get(0).equals(2L));
        a.check("第 2 行是 l.id=3（未匹配补 NULL 保留行）", isnull.get(1).get(0).equals(3L));
        a.check("保留行右列补 NULL", isnull.get(1).get(2) == null);

        // 这个谓词若被错误下推到右表（扫描前过滤 r.b IS NULL），结果会只剩 l.id=2，丢掉 l.id=3
        List<Row> eq1 = new Executor().execute(
                new Plan.Filter(ExprParser.parse("r.b = 1"), lj));
        a.check("r.b = 1 无命中（右表没有 b=1）", eq1.isEmpty());

        // 穷举重写器在该库上的决策与手工结果一致
        PushdownRewriter.RewriteResult rr = new PushdownRewriter().rewrite(
                new Plan.Filter(ExprParser.parse("r.b IS NULL"), lj));
        List<Row> after = new Executor().execute(rr.plan());
        a.check("重写后仍为 2 行且不丢保留行", QueryEngine.bagEquals(isnull, after));
    }

    // ------------------------------------------------------------------
    // 重名列
    // ------------------------------------------------------------------

    private static void duplicateColumnAssertions(Assert a) {
        a.section("穷举：重名列处理");
        Table l = t("l", List.of("id", "x"),
                new Object[]{1L, 1L}, new Object[]{2L, null});
        Table r = t("r", List.of("id", "x"),
                new Object[]{1L, null}, new Object[]{2L, 2L});
        Plan join = new Plan.LeftJoin(new Plan.Scan("l", l), new Plan.Scan("r", r),
                List.of(ExprParser.parse("l.id = r.id")));

        // 限定名分别下推，互不干扰
        PushdownRewriter.RewriteResult rr1 = new PushdownRewriter().rewrite(
                new Plan.Filter(ExprParser.parse("l.x = 1 AND r.x IS NULL"), join));
        List<Row> rows1 = new Executor().execute(rr1.plan());
        a.check("重名列：l.x=1 AND r.x IS NULL => 1 行(l.id=1)",
                rows1.size() == 1 && rows1.get(0).get(0).equals(1L));
        a.check("重名列：r.x IS NULL 未被下推（保留行语义）",
                rr1.plan() instanceof Plan.Filter f && f.child() instanceof Plan.LeftJoin);

        // 裸列名必须报歧义
        boolean threw = false;
        try {
            new PushdownRewriter().rewrite(
                    new Plan.Filter(ExprParser.parse("x = 1"), join));
        } catch (EngineException e) {
            threw = e.getMessage().contains("歧义");
        }
        a.check("重名列裸引用 x 报歧义错误", threw);

        // 内连接场景：限定名分别下推到左右
        Plan ij = new Plan.InnerJoin(new Plan.Scan("l", l), new Plan.Scan("r", r),
                List.of(ExprParser.parse("l.id = r.id")));
        PushdownRewriter.RewriteResult rr2 = new PushdownRewriter().rewrite(
                new Plan.Filter(ExprParser.parse("l.x = 1 AND r.x = 2"), ij));
        List<Row> rows2 = new Executor().execute(rr2.plan());
        List<Row> expect2 = new Executor().execute(
                new Plan.Filter(ExprParser.parse("l.x = 1 AND r.x = 2"), ij));
        a.check("重名列内连接：改写前后一致", QueryEngine.bagEquals(rows2, expect2));
        a.check("重名列内连接：左谓词下推到 Scan(l) 上方", filterOnScan(rr2.plan(), "l"));
        a.check("重名列内连接：右谓词下推到 Scan(r) 上方", filterOnScan(rr2.plan(), "r"));
        a.check("重名列内连接：连接上方无残留 Filter",
                rr2.plan() instanceof Plan.InnerJoin);
    }

    // ------------------------------------------------------------------

    private static boolean containsNode(Plan p, Class<?> type) {
        if (type.isInstance(p)) return true;
        for (Plan c : p.children()) {
            if (containsNode(c, type)) return true;
        }
        return false;
    }

    private static boolean filterOnScan(Plan p, String tableQualifier) {
        if (p instanceof Plan.Filter f
                && f.child() instanceof Plan.Scan s
                && s.tableData().qualifier().equals(tableQualifier)) {
            return true;
        }
        for (Plan c : p.children()) {
            if (filterOnScan(c, tableQualifier)) return true;
        }
        return false;
    }

    /** 枚举一张两列两行表的全部 3^4 种取值。 */
    private static List<Table> databases(String qualifier) {
        List<Table> out = new ArrayList<>();
        String col = qualifier.equals("l") ? "a" : "b";
        for (Object c0 : DOMAIN)
            for (Object c1 : DOMAIN)
                for (Object c2 : DOMAIN)
                    for (Object c3 : DOMAIN) {
                        Row r1 = new Row(java.util.Arrays.asList(c0, c1));
                        Row r2 = new Row(java.util.Arrays.asList(c2, c3));
                        out.add(new Table(qualifier, List.of("id", col), List.of(r1, r2)));
                    }
        return out;
    }

    private static Table t(String q, List<String> cols, Object[]... rows) {
        return ExecutorTest.t(q, cols, rows);
    }
}
