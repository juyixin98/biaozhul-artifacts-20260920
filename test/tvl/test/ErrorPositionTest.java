package tvl.test;

import tvl.analyzer.DataType;
import tvl.analyzer.Schema;
import tvl.analyzer.TypeChecker;
import tvl.analyzer.TypeException;
import tvl.api.ErrorFormatter;
import tvl.evaluator.EvalException;
import tvl.evaluator.Evaluator;
import tvl.evaluator.RowLike;
import tvl.expr.ExpressionException;
import tvl.lexer.LexException;
import tvl.parser.ParseException;
import tvl.parser.Parser;

import java.util.LinkedHashMap;
import java.util.Map;

/** 验证词法/语法/类型/运行期错误都精确定位到字符偏移与行列。 */
public class ErrorPositionTest extends TestBase {

    private static RowLike row(Object... kv) {
        Map<String, Object> map = new LinkedHashMap<>();
        for (int i = 0; i < kv.length; i += 2) {
            map.put((String) kv[i], kv[i + 1]);
        }
        return map::get;
    }

    @Override
    public String name() {
        return "Error positions (offset/line/column)";
    }

    @Override
    public void run() {
        // 词法错误：非法字符 @ 位于偏移 5（"a OR " 长度为5）
        expectOffset(LexException.class, "a OR @b", 5, "illegal char");
        // 未闭合字符串
        expectOffset(LexException.class, "'abc", 0, "unterminated string");
        // 整数字面量溢出（超过 Long.MAX 的最小数字串）
        expectOffset(LexException.class, "99999999999999999999999", 0, "integer overflow lex");

        // 语法错误：未知标记的位置
        expectOffset(ParseException.class, "a AND )", 6, "unexpected token");
        expectOffset(ParseException.class, "a +", 3, "dangling operator");
        expectOffset(ParseException.class, "(a", 2, "missing close paren at EOF");
        expectOffset(ParseException.class, "a < 1 < 2", 6, "chained comparison at second <");

        // 类型错误：未知列指向列名起点
        expectTypeError("unknown_col = 1", 0, "unknown column points at its name");
        // "a AND 1"：AND 右侧必须布尔，整数 1 的位置是 6
        expectTypeError("a AND 1", 6, "non-boolean operand points at '1'");
        // 算术遇字符串：b + 'x' 中 'x' 偏移 4
        expectTypeError("b + 'x'", 4, "string in arithmetic points at string literal");
        // 比较两侧类型不兼容：整数 = 字符串，错误指向 '=' 偏移 2
        expectTypeError("b = 'x'", 2, "incompatible comparison points at operator");
        // 布尔排序：a < b 中 a 为布尔，错误指向 '<' 偏移 2
        expectTypeError("a < flag", 2, "ordering boolean points at operator");

        // 求值期错误位置：指向具体运算节点（偏移从 0 计）
        expectEvalOffset("9223372036854775807 + 1", 20, "overflow points at +");
        expectEvalOffset("1 / 0", 2, "division by zero points at /");
        expectEvalOffset("1 + 2 / 0", 6, "nested div zero points at /");
        expectEvalOffset("-(-9223372036854775808)", 0, "negate overflow points at outer -");

        // 行列换算与指示符
        String multiLine = "a = 1\nAND b = @";
        int at = multiLine.indexOf('@');
        int[] lc = ErrorFormatter.lineAndColumn(multiLine, at);
        expectEq(2, lc[0], "@ is on line 2");
        expectEq(9, lc[1], "@ is column 9");
        String caret = ErrorFormatter.caret(multiLine, at);
        check(caret.contains("AND b = @") && caret.contains("^"), "caret snippet produced");
    }

    private <E extends ExpressionException> void expectOffset(
            Class<E> type, String source, int expectedOffset, String label) {
        try {
            Parser.parse(source);
            fail(label + " — expected " + type.getSimpleName() + " for <" + source + ">");
        } catch (ExpressionException ex) {
            if (type.isInstance(ex)) {
                expectEq(expectedOffset, ex.getPosition(),
                        label + " offset in <" + source + ">");
            } else {
                fail(label + " — expected " + type.getSimpleName()
                        + " but got " + ex.getClass().getSimpleName());
            }
        }
    }

    private void expectTypeError(String source, int expectedOffset, String label) {
        Schema schema = new Schema();
        schema.add("a", DataType.BOOLEAN);
        schema.add("flag", DataType.BOOLEAN);
        schema.add("b", DataType.INTEGER);
        schema.add("s", DataType.STRING);
        try {
            new TypeChecker(schema).check(Parser.parse(source));
            fail(label + " — expected TypeException for <" + source + ">");
        } catch (TypeException ex) {
            expectEq(expectedOffset, ex.getPosition(),
                    label + " offset in <" + source + ">");
        }
    }

    private void expectEvalOffset(String source, int expectedOffset, String label) {
        try {
            new Evaluator().evalScalar(Parser.parse(source), row());
            fail(label + " — expected EvalException");
        } catch (EvalException ex) {
            expectEq(expectedOffset, ex.getPosition(),
                    label + " offset in <" + source + ">");
        }
    }
}
