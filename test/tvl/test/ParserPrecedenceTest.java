package tvl.test;

import tvl.expr.Expr;
import tvl.parser.ParseException;
import tvl.parser.Parser;

/**
 * 运算符优先级与结合性测试：把 AST 渲染成 S-表达式进行结构断言。
 */
public class ParserPrecedenceTest extends TestBase {

    @Override
    public String name() {
        return "Parser precedence / grammar";
    }

    @Override
    public void run() {
        // OR 低于 AND
        expectEq("(OR a (AND b c))", sexpr("a OR b AND c"), "OR < AND");
        // AND 低于 NOT
        expectEq("(AND (NOT a) b)", sexpr("NOT a AND b"), "NOT > AND");
        // 比较低于算术
        expectEq("(= (+ a b) (* c d))", sexpr("a + b = c * d"), "arith below comparison");
        // 乘除高于加减，同级左结合
        expectEq("(- (+ a (* b c)) d)", sexpr("a + b * c - d"), "* over +, left-assoc");
        expectEq("(/ (* a b) c)", sexpr("a * b / c"), "*/ left-assoc");
        expectEq("(+ a (- b))", sexpr("a + -b"), "unary minus");
        expectEq("(- (- a))", sexpr("--a"), "double unary minus");
        // NOT 作用于比较谓词整体
        expectEq("(NOT (= a b))", sexpr("NOT a = b"), "NOT binds the comparison");
        expectEq("(NOT (= a b))", sexpr("NOT (a = b)"), "NOT (a=b)");
        // 括号改变结构
        expectEq("(AND (OR a b) c)", sexpr("(a OR b) AND c"), "parentheses override");
        // IS NULL 作用于整体算术/比较优先级层
        expectEq("(ISNULL (+ a b) false)", sexpr("a + b IS NULL"), "IS NULL on additive");
        expectEq("(ISNULL a true)", sexpr("a IS NOT NULL"), "IS NOT NULL");
        expectEq("(ISNULL (= a b) false)", sexpr("(a = b) IS NULL"), "IS NULL over comparison");
        // 关键字大小写不敏感，列名大小写敏感
        expectEq("(AND Aa bB)", sexpr("Aa aNd bB"), "keywords case-insensitive");
        // 比较符别名
        expectEq("(<> a b)", sexpr("a != b"), "!= maps to <>");
        expectEq("(= a b)", sexpr("a == b"), "== maps to =");
        // 链式比较不允许
        expectParseError("a < b < c", "chained comparison");
        // 未闭合括号 / 悬空运算符
        expectParseError("(a AND b", "unclosed paren");
        expectParseError("a AND", "trailing AND");
        expectParseError("a + )", "unexpected )");
        expectParseError("* 3", "leading *");
        // IS 后面必须跟 NULL
        expectParseError("a IS b", "IS without NULL");
        // 空表达式
        expectParseError("", "empty expression");
        expectParseError("   ", "whitespace only");
    }

    private String sexpr(String source) {
        return render(Parser.parse(source));
    }

    private void expectParseError(String source, String label) {
        try {
            Parser.parse(source);
            fail(label + " — expected ParseException for <" + source + ">");
        } catch (ParseException ex) {
            check(true, label + " (" + ex.getMessage() + ")");
        }
    }

    /** 渲染 AST 为规范 S-表达式。 */
    static String render(Expr e) {
        return switch (e) {
            case Expr.Literal lit -> switch (lit.kind) {
                case INTEGER -> String.valueOf(lit.value);
                case STRING -> "'" + lit.value + "'";
                case BOOLEAN -> String.valueOf(lit.value).toUpperCase();
                case NULL -> "NULL";
                case UNKNOWN -> "UNKNOWN";
            };
            case Expr.Column c -> c.name;
            case Expr.UnaryMinus um -> "(- " + render(um.operand) + ")";
            case Expr.BinaryArith b -> "(" + shortOp(b.op) + " "
                    + render(b.left) + " " + render(b.right) + ")";
            case Expr.Compare c -> "(" + c.op.symbol + " " + render(c.left) + " "
                    + render(c.right) + ")";
            case Expr.IsNull isn -> "(ISNULL " + render(isn.operand) + " "
                    + isn.negated + ")";
            case Expr.Not n -> "(NOT " + render(n.operand) + ")";
            case Expr.Logical l -> "(" + l.op.name() + " " + render(l.left) + " "
                    + render(l.right) + ")";
        };
    }

    private static String shortOp(Expr.ArithOp op) {
        return switch (op) {
            case ADD -> "+";
            case SUB -> "-";
            case MUL -> "*";
            case DIV -> "/";
        };
    }
}
