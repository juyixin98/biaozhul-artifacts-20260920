package tvl.test;

import java.util.LinkedHashMap;
import java.util.Map;

import tvl.evaluator.EvalException;
import tvl.evaluator.Evaluator;
import tvl.evaluator.RowLike;
import tvl.expr.Tri;
import tvl.parser.Parser;

/** 求值语义：NULL 传播、字符串比较、溢出、除零、IS NULL 与复合表达式。 */
public class EvaluatorSemanticsTest extends TestBase {

    private final Evaluator evaluator = new Evaluator();

    private static RowLike row(Object... kv) {
        Map<String, Object> map = new LinkedHashMap<>();
        for (int i = 0; i < kv.length; i += 2) {
            map.put((String) kv[i], kv[i + 1]);
        }
        return map::get;
    }

    @Override
    public String name() {
        return "Evaluator semantics";
    }

    @Override
    public void run() {
        nullArithmetic();
        integerArithmetic();
        overflow();
        division();
        stringsAndBooleans();
        compounds();
        isNullOnExpressions();
    }

    private void nullArithmetic() {
        // 任一侧 NULL -> 结果 NULL
        expectScalar(null, "i + 1", row("i", null), "NULL + 1");
        expectScalar(null, "1 + i", row("i", null), "1 + NULL");
        expectScalar(null, "i * j", row("i", 2L, "j", null), "2 * NULL");
        expectScalar(null, "-i", row("i", null), "-NULL");
        // NULL 算术短路掉右值除零（与大多数 SQL 一致：左侧 NULL 时结果已是 NULL）
        expectScalar(null, "i + (1/0)", row("i", null), "NULL + 1/0 -> NULL without error");
    }

    private void integerArithmetic() {
        expectScalar(7L, "3 + 4", row(), "3+4");
        expectScalar(-1L, "3 - 4", row(), "3-4");
        expectScalar(12L, "3 * 4", row(), "3*4");
        expectScalar(2L, "10 / 4", row(), "10/4 truncates toward zero");
        expectScalar(-2L, "-10 / 4", row(), "-10/4 truncates toward zero");
        expectScalar(3L, "1 + 2 * 3 - 4", row(), "precedence 1+2*3-4");
        expectScalar(-5L, "-(2+3)", row(), "-(2+3)");
    }

    private void overflow() {
        expectEvalError("9223372036854775807 + 1", "max + 1 overflow");
        expectEvalError("-9223372036854775808 - 1", "min - 1 overflow");
        expectEvalError("9223372036854775807 * 2", "max * 2 overflow");
        expectEvalError("-9223372036854775808 * -1", "min * -1 overflow");
        expectEvalError("-(-9223372036854775808)", "negate min overflow");
    }

    private void division() {
        expectEvalError("1 / 0", "divide by zero");
        expectEvalError("0 / 0", "0 / 0");
        expectEvalError("-9223372036854775808 / -1", "MIN/-1 overflow");
    }

    private void stringsAndBooleans() {
        expectPredicate(Tri.TRUE, "'abc' = 'abc'", row(), "string equality");
        expectPredicate(Tri.TRUE, "'abc' < 'abd'", row(), "string ordering");
        expectPredicate(Tri.TRUE, "'B' < 'a'", row(), "lexicographic by UTF-16 code unit");
        expectPredicate(Tri.TRUE, "TRUE = TRUE", row(), "boolean equality");
        expectPredicate(Tri.FALSE, "TRUE = FALSE", row(), "boolean inequality value");
        expectPredicate(Tri.TRUE, "TRUE <> FALSE", row(), "boolean <>");
    }

    private void compounds() {
        // (i > 0 AND NOT flag) OR name IS NULL 的全部关键分支
        String expr = "(i > 0 AND NOT flag) OR name IS NULL";
        expectPredicate(Tri.TRUE, expr, row("i", 5L, "flag", false, "name", "x"),
                "T AND T -> T branch");
        expectPredicate(Tri.FALSE, expr, row("i", 5L, "flag", true, "name", "x"),
                "T AND F, name not null -> F");
        expectPredicate(Tri.TRUE, expr, row("i", -1L, "flag", true, "name", null),
                "first clause F, name IS NULL -> T");
        expectPredicate(Tri.UNKNOWN, expr, row("i", null, "flag", false, "name", "x"),
                "UNKNOWN propagated -> UNKNOWN");
        // NOT 与比较
        expectPredicate(Tri.TRUE, "NOT (1 = 2)", row(), "NOT false");
        expectPredicate(Tri.FALSE, "NOT (1 <> 2)", row(), "NOT true");
        expectPredicate(Tri.UNKNOWN, "NOT (1 = i)", row("i", null), "NOT UNKNOWN");
    }

    private void isNullOnExpressions() {
        expectPredicate(Tri.TRUE, "(i + j) IS NULL", row("i", 1L, "j", null),
                "result NULL -> IS NULL");
        expectPredicate(Tri.FALSE, "(i + j) IS NOT NULL", row("i", 1L, "j", null),
                "result NULL -> IS NOT NULL false");
        expectPredicate(Tri.TRUE, "(i / j) IS NULL", row("i", 1L, "j", null),
                "NULL divisor yields NULL, not division error");
    }

    private void expectScalar(Object expected, String source, RowLike r, String label) {
        try {
            Object actual = evaluator.evalScalar(Parser.parse(source), r);
            expectEq(expected, actual, label);
        } catch (EvalException ex) {
            fail(label + " — unexpected EvalException: " + ex.getMessage());
        }
    }

    private void expectPredicate(Tri expected, String source, RowLike r, String label) {
        try {
            expectEq(expected, evaluator.evalPredicate(Parser.parse(source), r), label);
        } catch (EvalException ex) {
            fail(label + " — unexpected EvalException: " + ex.getMessage());
        }
    }

    private void expectEvalError(String source, String label) {
        try {
            evaluator.evalScalar(Parser.parse(source), row());
            fail(label + " — expected EvalException");
        } catch (EvalException ex) {
            check(true, label + ": " + ex.getMessage());
        }
    }
}
