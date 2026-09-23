package ppd.tests;

import ppd.EngineException;
import ppd.Expr;
import ppd.ExprParser;
import ppd.RefKey;
import ppd.Values;

import java.util.Set;

/** 表达式解析、三值逻辑求值与 NULL 拒绝性分析的单元测试。 */
public class ExprTest {

    private static Expr.Env envOf(Object rb) {
        return (q, n) -> rb;
    }

    private static Object eval(Expr e, Object rv) {
        return e.eval((q, n) -> {
            if (n.equals("b") && (q == null || q.equals("r"))) return rv;
            if (n.equals("a") && (q == null || q.equals("l"))) return rv;
            return null;
        });
    }

    public static void run(Assert a) {
        a.section("表达式：解析与三值逻辑");

        // 比较的三值逻辑
        Expr gt = ExprParser.parse("r.b > 50");
        a.check("50<NULL 比较 => UNKNOWN", eval(gt, null) == null);
        a.check("100>50 => TRUE", Boolean.TRUE.equals(eval(gt, 100L)));
        a.check("10>50 => FALSE", Boolean.FALSE.equals(eval(gt, 10L)));

        Expr eq = ExprParser.parse("r.b = 1");
        a.check("NULL=1 => UNKNOWN", eval(eq, null) == null);
        a.check("1=1 => TRUE", Boolean.TRUE.equals(eval(eq, 1L)));

        Expr ne = ExprParser.parse("r.b <> 1");
        a.check("NULL<>1 => UNKNOWN", eval(ne, null) == null);
        a.check("2<>1 => TRUE", Boolean.TRUE.equals(eval(ne, 2L)));

        Expr isnull = ExprParser.parse("r.b IS NULL");
        a.check("NULL IS NULL => TRUE", Boolean.TRUE.equals(eval(isnull, null)));
        a.check("1 IS NULL => FALSE", Boolean.FALSE.equals(eval(isnull, 1L)));

        Expr notnull = ExprParser.parse("r.b IS NOT NULL");
        a.check("NULL IS NOT NULL => FALSE", Boolean.FALSE.equals(eval(notnull, null)));
        a.check("1 IS NOT NULL => TRUE", Boolean.TRUE.equals(eval(notnull, 1L)));

        Expr andExpr = ExprParser.parse("(r.b > 1) AND (1 = 2)");
        a.check("AND: UNKNOWN AND FALSE => FALSE",
                Boolean.FALSE.equals(eval(andExpr, null)));
        a.check("AND: 5>1 AND TRUE => TRUE",
                Boolean.TRUE.equals(ExprParser.parse("(r.b > 1) AND (r.b < 10)")
                        .eval(envOf(5L))));
        a.check("AND: UNKNOWN AND TRUE => UNKNOWN",
                ExprParser.parse("(r.b > 1) AND (1 = 1)").eval(envOf(null)) == null);

        Expr orExpr = ExprParser.parse("(r.b = 1) OR (r.b = 2)");
        a.check("OR: NULL OR NULL => UNKNOWN", eval(orExpr, null) == null);
        a.check("OR: 2 命中 => TRUE", Boolean.TRUE.equals(eval(orExpr, 2L)));

        Expr notExpr = ExprParser.parse("NOT (r.b = 1)");
        a.check("NOT UNKNOWN => UNKNOWN", eval(notExpr, null) == null);
        a.check("NOT TRUE => FALSE", Boolean.FALSE.equals(eval(notExpr, 1L)));

        // 算术与字符串
        Expr add = ExprParser.parse("r.b + 1");
        a.check("NULL+1 => NULL", eval(add, null) == null);
        a.check("41+1 => 42 (Long)", Long.valueOf(42L).equals(eval(add, 41L)));
        Expr concat = ExprParser.parse("'x' + r.b");
        a.check("字符串拼接", "x1".equals(eval(concat, 1L)));

        // 解析往返
        String[] roundTrip = {
                "(l.a = r.b)", "(r.b IS NOT NULL)", "((r.b > 1) AND (r.b < 10))",
                "((r.b = 1) OR (r.b = 2))", "(NOT (r.b = 1))", "((r.b + 1) * 2)"
        };
        for (String s : roundTrip) {
            Expr e = ExprParser.parse(s);
            Expr again = ExprParser.parse(e.toSql());
            a.check("往返解析: " + s, e.toSql().equals(again.toSql()));
        }

        // 整数不应被解析成浮点
        Expr.Cmp litCmp = (Expr.Cmp) gt;
        a.check("整数字面量保持 Long",
                ((Expr.Lit) litCmp.right()).value() instanceof Long);

        // 括号与优先级
        Expr mix = ExprParser.parse("r.b = 1 OR r.b = 2 AND r.b > 0");
        a.check("AND 优先级高于 OR",
                mix instanceof Expr.Logic(Expr.LogicOp op, Expr l, Expr r)
                        && op == Expr.LogicOp.OR
                        && r instanceof Expr.Logic(Expr.LogicOp op2, Expr x, Expr y)
                        && op2 == Expr.LogicOp.AND);

        a.section("表达式：NULL 拒绝性判定（保留行模型）");
        RefKey rb = new RefKey("r", "b");
        RefKey la = new RefKey("l", "a");

        rejects(a, "r.b > 50", Set.of(rb), true);
        rejects(a, "r.b = 1", Set.of(rb), true);
        rejects(a, "r.b <> 1", Set.of(rb), true);
        rejects(a, "r.b + 1 = 2", Set.of(rb), true);
        rejects(a, "r.b IS NULL", Set.of(rb), false);
        rejects(a, "r.b IS NOT NULL", Set.of(rb), true);
        rejects(a, "(r.b = 1) OR (r.b = 2)", Set.of(rb), true);
        rejects(a, "(r.b > 1) AND (r.b < 10)", Set.of(rb), true);
        // 含 IS NULL 的析取：保留行可能满足 => 不拒绝
        rejects(a, "(r.b > 50) OR (r.b IS NULL)", Set.of(rb), false);
        // IS NOT NULL 合取无影响（本就拒绝）
        rejects(a, "(r.b > 50) AND (r.b IS NOT NULL)", Set.of(rb), true);
        // NOT r.b IS NULL 等价 IS NOT NULL
        rejects(a, "NOT (r.b IS NULL)", Set.of(rb), true);
        // NOT(r.b > 50)：UNKNOWN 取反仍 UNKNOWN => 拒绝
        rejects(a, "NOT (r.b > 50)", Set.of(rb), true);
        // 常量谓词
        rejects(a, "FALSE", Set.of(rb), true);
        rejects(a, "TRUE", Set.of(rb), false);
        rejects(a, "1 = 1", Set.of(rb), false);
        // 只引用左表列：右列全 NULL 与其无关 => 不“因右表 NULL”被拒绝
        rejects(a, "l.a = 1", Set.of(rb), false);
        rejects(a, "l.a = 1", Set.of(la), true);
        // 跨两侧谓词
        rejects(a, "l.a = r.b", Set.of(rb), true);
        rejects(a, "(l.a = r.b) OR (r.b IS NULL)", Set.of(rb), false);

        // Values 工具
        a.section("表达式：Values 工具");
        a.check("Long/Double 数值相等", Values.eq(1L, 1.0) == Boolean.TRUE);
        a.check("null 相等 => null", Values.eq(null, 1) == null);
        a.check("跨类型 eq => false", Values.eq(1L, "1") == Boolean.FALSE);
        a.check("字符串排序", Values.compare("a", "b") == -1);
        a.check("数值比较 Long/Double", Values.compare(2L, 1.5) == 1);
        boolean threw = false;
        try { Values.compare(1L, "x"); } catch (EngineException e) { threw = true; }
        a.check("不可比较类型抛 EngineException", threw);
    }

    private static void rejects(Assert a, String text, Set<RefKey> nulled, boolean expected) {
        Expr e = ExprParser.parse(text);
        a.check("rejectsNulls(" + text + ")=" + expected,
                e.rejectsNulls(nulled) == expected);
    }
}
