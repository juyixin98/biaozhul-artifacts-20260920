package tvl.test;

import java.util.LinkedHashMap;
import java.util.Map;

import tvl.evaluator.EvalException;
import tvl.evaluator.Evaluator;
import tvl.evaluator.RowLike;
import tvl.expr.Expr;
import tvl.expr.Tri;
import tvl.parser.Parser;

/**
 * 穷举三值逻辑真值表（AND/OR 各 3x3=9 格，NOT 3 格），
 * 并覆盖 IS NULL 真值表、短路除零与 NULL 参与比较。
 */
public class TruthTablesTest extends TestBase {

    private final Evaluator evaluator = new Evaluator();

    /** 用内存 Map 实现行，null 即 SQL NULL。 */
    private static RowLike row(Object... kv) {
        Map<String, Object> map = new LinkedHashMap<>();
        for (int i = 0; i < kv.length; i += 2) {
            map.put((String) kv[i], kv[i + 1]);
        }
        return map::get;
    }

    private static final Boolean T = Boolean.TRUE;
    private static final Boolean F = Boolean.FALSE;

    @Override
    public String name() {
        return "Exhaustive 3VL truth tables";
    }

    @Override
    public void run() {
        andTruthTable();
        orTruthTable();
        notTruthTable();
        isNullTruthTable();
        shortCircuit();
        nullComparisons();
        literalUnknown();
    }

    private void andTruthTable() {
        // 列 a、b 取值：TRUE / FALSE / null(UNKNOWN)
        Boolean[] values = {T, F, null};
        // SQL AND 参考真值表，索引顺序 TRUE, FALSE, UNKNOWN
        Tri[][] expected = {
                {Tri.TRUE,  Tri.FALSE,   Tri.UNKNOWN}, // a = TRUE
                {Tri.FALSE, Tri.FALSE,   Tri.FALSE},   // a = FALSE
                {Tri.UNKNOWN, Tri.FALSE, Tri.UNKNOWN}, // a = UNKNOWN
        };
        Expr expr = Parser.parse("a AND b");
        for (int i = 0; i < 3; i++) {
            for (int j = 0; j < 3; j++) {
                Tri actual = evaluator.evalPredicate(expr, row("a", values[i], "b", values[j]));
                expectEq(expected[i][j], actual,
                        "AND(" + triName(values[i]) + "," + triName(values[j]) + ")");
            }
        }
    }

    private void orTruthTable() {
        Boolean[] values = {T, F, null};
        Tri[][] expected = {
                {Tri.TRUE,  Tri.TRUE,  Tri.TRUE},    // a = TRUE
                {Tri.TRUE,  Tri.FALSE, Tri.UNKNOWN}, // a = FALSE
                {Tri.TRUE,  Tri.UNKNOWN, Tri.UNKNOWN}, // a = UNKNOWN
        };
        Expr expr = Parser.parse("a OR b");
        for (int i = 0; i < 3; i++) {
            for (int j = 0; j < 3; j++) {
                Tri actual = evaluator.evalPredicate(expr, row("a", values[i], "b", values[j]));
                expectEq(expected[i][j], actual,
                        "OR(" + triName(values[i]) + "," + triName(values[j]) + ")");
            }
        }
    }

    private void notTruthTable() {
        Boolean[] values = {T, F, null};
        Tri[] expected = {Tri.FALSE, Tri.TRUE, Tri.UNKNOWN};
        Expr expr = Parser.parse("NOT a");
        for (int i = 0; i < 3; i++) {
            Tri actual = evaluator.evalPredicate(expr, row("a", values[i]));
            expectEq(expected[i], actual, "NOT(" + triName(values[i]) + ")");
        }
    }

    private void isNullTruthTable() {
        // x 取非空整数与 NULL，穷举 IS NULL / IS NOT NULL
        Expr isNull = Parser.parse("x IS NULL");
        Expr isNotNull = Parser.parse("x IS NOT NULL");
        expectEq(Tri.TRUE, evaluator.evalPredicate(isNull, row("x", null)), "NULL IS NULL");
        expectEq(Tri.FALSE, evaluator.evalPredicate(isNull, row("x", 5L)), "5 IS NULL");
        expectEq(Tri.FALSE, evaluator.evalPredicate(isNotNull, row("x", null)), "NULL IS NOT NULL");
        expectEq(Tri.TRUE, evaluator.evalPredicate(isNotNull, row("x", 5L)), "5 IS NOT NULL");
        // IS NULL 绝不受 3VL UNKNOWN 影响
        Expr complex = Parser.parse("(x = 1) IS NULL");
        expectEq(Tri.TRUE, evaluator.evalPredicate(complex, row("x", null)), "(NULL=1) IS NULL");
        expectEq(Tri.FALSE, evaluator.evalPredicate(complex, row("x", 1L)), "(1=1) IS NULL");
    }

    private void shortCircuit() {
        // 右操作数含除零：1 / x = 1
        Expr and = Parser.parse("a AND (1 / x = 1)");
        Expr or = Parser.parse("a OR (1 / x = 1)");

        // AND 左值 FALSE -> 短路，右值（含除零）绝不求值
        expectEq(Tri.FALSE, evaluator.evalPredicate(and, row("a", F, "x", 0L)),
                "FALSE AND 1/0 -> FALSE, no error (short-circuit)");
        // AND 左值 TRUE -> 必须求值右值 -> 除零错误
        expectThrowsEval("TRUE AND 1/0", () ->
                evaluator.evalPredicate(and, row("a", T, "x", 0L)));
        // AND 左值 UNKNOWN -> 结果取决于右值，必须求值 -> 除零错误（不短路）
        expectThrowsEval("UNKNOWN AND 1/0", () ->
                evaluator.evalPredicate(and, row("a", null, "x", 0L)));
        // 左 UNKNOWN、右值正常求值为 TRUE -> UNKNOWN AND TRUE => UNKNOWN
        expectEq(Tri.UNKNOWN, evaluator.evalPredicate(and, row("a", null, "x", 1L)),
                "UNKNOWN AND TRUE -> UNKNOWN");

        // OR 左值 TRUE -> 短路
        expectEq(Tri.TRUE, evaluator.evalPredicate(or, row("a", T, "x", 0L)),
                "TRUE OR 1/0 -> TRUE, no error (short-circuit)");
        // OR 左值 FALSE -> 求值右值 -> 除零
        expectThrowsEval("FALSE OR 1/0", () ->
                evaluator.evalPredicate(or, row("a", F, "x", 0L)));
        // OR 左值 UNKNOWN -> 求值右值 -> 除零（不短路）
        expectThrowsEval("UNKNOWN OR 1/0", () ->
                evaluator.evalPredicate(or, row("a", null, "x", 0L)));
        // UNKNOWN OR FALSE -> UNKNOWN
        Expr orFalse = Parser.parse("a OR (x = 2)");
        expectEq(Tri.UNKNOWN, evaluator.evalPredicate(orFalse, row("a", null, "x", 1L)),
                "UNKNOWN OR FALSE -> UNKNOWN");
        // UNKNOWN OR TRUE -> TRUE
        expectEq(Tri.TRUE, evaluator.evalPredicate(orFalse, row("a", null, "x", 2L)),
                "UNKNOWN OR TRUE -> TRUE");
    }

    private void nullComparisons() {
        // NULL 参与任何比较都为 UNKNOWN：穷举六个比较符 x {op} y，其中左/右为 NULL
        String[] ops = {"=", "<>", "<", "<=", ">", ">="};
        for (String op : ops) {
            Expr leftNull = Parser.parse("x " + op + " 1");
            expectEq(Tri.UNKNOWN, evaluator.evalPredicate(leftNull, row("x", null)),
                    "NULL " + op + " 1 -> UNKNOWN");
            Expr rightNull = Parser.parse("1 " + op + " x");
            expectEq(Tri.UNKNOWN, evaluator.evalPredicate(rightNull, row("x", null)),
                    "1 " + op + " NULL -> UNKNOWN");
            Expr bothNull = Parser.parse("x " + op + " y");
            expectEq(Tri.UNKNOWN, evaluator.evalPredicate(bothNull, row("x", null, "y", null)),
                    "NULL " + op + " NULL -> UNKNOWN (SQL: never TRUE, not even = )");
        }
        // 非空整数比较的穷举小表（-1,0,1 两两组合，六种运算符）
        long[] nums = {-1L, 0L, 1L};
        for (long a : nums) {
            for (long b : nums) {
                expectEq(Tri.fromBoolean(a == b),
                        evaluator.evalPredicate(Parser.parse("a = b"), row("a", a, "b", b)),
                        a + " = " + b);
                expectEq(Tri.fromBoolean(a != b),
                        evaluator.evalPredicate(Parser.parse("a <> b"), row("a", a, "b", b)),
                        a + " <> " + b);
                expectEq(Tri.fromBoolean(a < b),
                        evaluator.evalPredicate(Parser.parse("a < b"), row("a", a, "b", b)),
                        a + " < " + b);
                expectEq(Tri.fromBoolean(a <= b),
                        evaluator.evalPredicate(Parser.parse("a <= b"), row("a", a, "b", b)),
                        a + " <= " + b);
                expectEq(Tri.fromBoolean(a > b),
                        evaluator.evalPredicate(Parser.parse("a > b"), row("a", a, "b", b)),
                        a + " > " + b);
                expectEq(Tri.fromBoolean(a >= b),
                        evaluator.evalPredicate(Parser.parse("a >= b"), row("a", a, "b", b)),
                        a + " >= " + b);
            }
        }
    }

    private void literalUnknown() {
        expectEq(Tri.UNKNOWN,
                evaluator.evalPredicate(Parser.parse("UNKNOWN"), row()), "UNKNOWN literal");
        expectEq(Tri.UNKNOWN,
                evaluator.evalPredicate(Parser.parse("NOT UNKNOWN"), row()), "NOT UNKNOWN");
        expectEq(Tri.UNKNOWN,
                evaluator.evalPredicate(Parser.parse("UNKNOWN AND TRUE"), row()),
                "UNKNOWN AND TRUE");
        expectEq(Tri.FALSE,
                evaluator.evalPredicate(Parser.parse("UNKNOWN AND FALSE"), row()),
                "UNKNOWN AND FALSE");
    }

    private void expectThrowsEval(String label, Runnable r) {
        try {
            r.run();
            fail(label + " — expected EvalException but none thrown");
        } catch (EvalException expected) {
            check(true, label);
        } catch (RuntimeException other) {
            fail(label + " — expected EvalException but got " + other.getClass().getSimpleName()
                    + ": " + other.getMessage());
        }
    }

    private static String triName(Boolean b) {
        return b == null ? "UNKNOWN" : (b ? "TRUE" : "FALSE");
    }
}
