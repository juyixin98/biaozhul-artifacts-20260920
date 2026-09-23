package tvl.test;

import tvl.analyzer.DataType;
import tvl.analyzer.Schema;
import tvl.analyzer.TypeChecker;
import tvl.analyzer.TypeException;
import tvl.expr.Expr;
import tvl.parser.Parser;

/** 类型检查器的接受/拒绝规则。 */
public class TypeCheckerTest extends TestBase {

    private final Schema schema = buildSchema();

    private static Schema buildSchema() {
        Schema s = new Schema();
        s.add("i", DataType.INTEGER);
        s.add("j", DataType.INTEGER);
        s.add("str", DataType.STRING);
        s.add("name", DataType.STRING);
        s.add("flag", DataType.BOOLEAN);
        return s;
    }

    @Override
    public String name() {
        return "Type checker";
    }

    @Override
    public void run() {
        // 合法表达式
        accepts("i = j", DataType.BOOLEAN);
        accepts("i + j * 2 > 10", DataType.BOOLEAN);
        accepts("(i + j) IS NULL", DataType.BOOLEAN);
        accepts("str = 'x'", DataType.BOOLEAN);
        accepts("str < name", DataType.BOOLEAN);
        accepts("flag", DataType.BOOLEAN);
        accepts("flag AND i = 1", DataType.BOOLEAN);
        accepts("NOT (flag OR i <> j)", DataType.BOOLEAN);
        accepts("-i + 3", DataType.INTEGER);
        // 与 NULL/UNKNOWN 字面量组合
        accepts("i = NULL", DataType.BOOLEAN);
        accepts("flag AND NULL", DataType.NULL);
        accepts("i + NULL", DataType.NULL);
        accepts("NOT NULL", DataType.NULL);
        accepts("flag AND UNKNOWN", DataType.BOOLEAN);

        // 非法表达式
        rejects("str + 1", "string + integer");
        rejects("flag + 1", "boolean arithmetic");
        rejects("i = str", "integer vs string");
        rejects("flag = i", "boolean vs integer");
        rejects("flag < TRUE", "boolean ordered");
        rejects("i AND j", "integer in AND");
        rejects("NOT i", "NOT integer");
        rejects("str OR flag", "string in OR");
        rejects("missing_col = 1", "unknown column");
        rejects("i +", "syntax before type check still reported");
    }

    private void accepts(String source, DataType expected) {
        try {
            DataType actual = new TypeChecker(schema).check(Parser.parse(source));
            expectEq(expected, actual, "accept <" + source + ">");
        } catch (RuntimeException ex) {
            fail("accept <" + source + "> but got " + ex.getClass().getSimpleName()
                    + ": " + ex.getMessage());
        }
    }

    private void rejects(String source, String label) {
        Expr expr;
        try {
            expr = Parser.parse(source);
        } catch (RuntimeException parseEx) {
            check(true, label + " (rejected at parse: " + parseEx.getMessage() + ")");
            return;
        }
        try {
            new TypeChecker(schema).check(expr);
            fail(label + " — expected TypeException for <" + source + ">");
        } catch (TypeException ex) {
            check(true, label + ": " + ex.getMessage());
        }
    }
}
