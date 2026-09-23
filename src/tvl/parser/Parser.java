package tvl.parser;

import tvl.expr.Expr;
import tvl.lexer.Lexer;
import tvl.lexer.Token;
import tvl.lexer.TokenType;

/**
 * 表达式递归下降解析器。
 *
 * 语法（优先级从低到高）：
 * <pre>
 * orExpr       := andExpr ( OR andExpr )*
 * andExpr      := notExpr ( AND notExpr )*
 * notExpr      := NOT notExpr | predicate
 * predicate    := comparison ( IS [NOT] NULL )?
 * comparison   := additive ((=|==|&lt;&gt;|!=|&lt;|&lt;=|&gt;|&gt;=) additive)?
 * additive     := term ((+ | -) term)*
 * term         := unary ((* | /) unary)*
 * unary        := -unary | primary
 * primary      := INTEGER | STRING | NULL | TRUE | FALSE | UNKNOWN | IDENT | '(' orExpr ')'
 * </pre>
 *
 * 不允许链式比较（如 {@code a < b < c}）；不允许比较结果做算术；
 * 这些约束的一部分在解析期发现，其余在类型检查期发现。
 */
public final class Parser {

    private final String source;
    private final Lexer lexer;
    private Token current;

    public Parser(String source) {
        this.source = source;
        this.lexer = new Lexer(source);
        this.current = lexer.next();
    }

    public static Expr parse(String source) {
        Parser p = new Parser(source);
        Expr expr = p.parseOr();
        if (p.current.type() != TokenType.EOF) {
            throw new ParseException(
                    "unexpected token '" + p.current.text() + "' after complete expression",
                    p.current.pos());
        }
        return expr;
    }

    private Expr parseOr() {
        Expr left = parseAnd();
        while (current.type() == TokenType.OR) {
            Token op = current;
            advance();
            Expr right = parseAnd();
            left = new Expr.Logical(Expr.LogicalOp.OR, left, right, op.pos());
        }
        return left;
    }

    private Expr parseAnd() {
        Expr left = parseNot();
        while (current.type() == TokenType.AND) {
            Token op = current;
            advance();
            Expr right = parseNot();
            left = new Expr.Logical(Expr.LogicalOp.AND, left, right, op.pos());
        }
        return left;
    }

    private Expr parseNot() {
        if (current.type() == TokenType.NOT) {
            Token op = current;
            advance();
            return new Expr.Not(parseNot(), op.pos());
        }
        return parsePredicate();
    }

    private Expr parsePredicate() {
        Expr expr = parseComparison();
        if (current.type() == TokenType.IS) {
            Token is = current;
            advance();
            boolean negated = false;
            if (current.type() == TokenType.NOT) {
                negated = true;
                advance();
            }
            if (current.type() != TokenType.NULL) {
                throw new ParseException(
                        "expected NULL after IS " + (negated ? "NOT " : ""),
                        current.pos());
            }
            advance();
            expr = new Expr.IsNull(expr, negated, is.pos());
        }
        return expr;
    }

    private Expr parseComparison() {
        Expr left = parseAdditive();
        Expr.CompareOp op = switch (current.type()) {
            case EQ -> Expr.CompareOp.EQ;
            case NE -> Expr.CompareOp.NE;
            case LT -> Expr.CompareOp.LT;
            case LE -> Expr.CompareOp.LE;
            case GT -> Expr.CompareOp.GT;
            case GE -> Expr.CompareOp.GE;
            default -> null;
        };
        if (op == null) {
            return left;
        }
        Token opToken = current;
        advance();
        Expr right = parseAdditive();
        Expr comparison = new Expr.Compare(op, left, right, opToken.pos());
        // 链式比较 a < b < c：第二个比较符在此处被发现
        if (isComparisonOperator(current.type())) {
            throw new ParseException(
                    "chained comparisons are not allowed (did you mean to combine them with AND?)",
                    current.pos());
        }
        return comparison;
    }

    private static boolean isComparisonOperator(TokenType t) {
        return t == TokenType.EQ || t == TokenType.NE || t == TokenType.LT
                || t == TokenType.LE || t == TokenType.GT || t == TokenType.GE;
    }

    private Expr parseAdditive() {
        Expr left = parseTerm();
        while (current.type() == TokenType.PLUS || current.type() == TokenType.MINUS) {
            Token op = current;
            advance();
            Expr right = parseTerm();
            Expr.ArithOp arithOp = op.type() == TokenType.PLUS
                    ? Expr.ArithOp.ADD : Expr.ArithOp.SUB;
            left = new Expr.BinaryArith(arithOp, left, right, op.pos());
        }
        return left;
    }

    private Expr parseTerm() {
        Expr left = parseUnary();
        while (current.type() == TokenType.STAR || current.type() == TokenType.SLASH) {
            Token op = current;
            advance();
            Expr right = parseUnary();
            Expr.ArithOp arithOp = op.type() == TokenType.STAR
                    ? Expr.ArithOp.MUL : Expr.ArithOp.DIV;
            left = new Expr.BinaryArith(arithOp, left, right, op.pos());
        }
        return left;
    }

    private Expr parseUnary() {
        if (current.type() == TokenType.MINUS) {
            Token op = current;
            advance();
            // 仅在负号直接作用于边界数字字面量时折叠为 Long.MIN_VALUE；
            // 带括号的 -(-9223372036854775808) 必须保留外层一元负号节点（其求值确实溢出）
            if (current.type() == TokenType.INTEGER
                    && tvl.lexer.Lexer.MIN_LONG_DIGITS.equals(current.text())) {
                advance();
                return new Expr.Literal(Expr.LiteralKind.INTEGER, Long.MIN_VALUE, op.pos());
            }
            return new Expr.UnaryMinus(parseUnary(), op.pos());
        }
        if (current.type() == TokenType.PLUS) {
            advance();
            return parseUnary();
        }
        return parsePrimary();
    }

    private Expr parsePrimary() {
        Token t = current;
        switch (t.type()) {
            case INTEGER -> {
                advance();
                return new Expr.Literal(Expr.LiteralKind.INTEGER, t.intVal(), t.pos());
            }
            case STRING -> {
                advance();
                return new Expr.Literal(Expr.LiteralKind.STRING, t.text(), t.pos());
            }
            case NULL -> {
                advance();
                return new Expr.Literal(Expr.LiteralKind.NULL, null, t.pos());
            }
            case TRUE -> {
                advance();
                return new Expr.Literal(Expr.LiteralKind.BOOLEAN, Boolean.TRUE, t.pos());
            }
            case FALSE -> {
                advance();
                return new Expr.Literal(Expr.LiteralKind.BOOLEAN, Boolean.FALSE, t.pos());
            }
            case UNKNOWN -> {
                advance();
                return new Expr.Literal(Expr.LiteralKind.UNKNOWN, null, t.pos());
            }
            case IDENT -> {
                advance();
                return new Expr.Column(t.text(), t.pos());
            }
            case LPAREN -> {
                advance();
                Expr inner = parseOr();
                expect(TokenType.RPAREN, "expected ')' to close parenthesised expression");
                return inner;
            }
            default -> throw new ParseException(
                    "unexpected token '" + t.text() + "', expected a value, column or '('",
                    t.pos());
        }
    }

    private void expect(TokenType type, String message) {
        if (current.type() != type) {
            throw new ParseException(message, current.pos());
        }
        advance();
    }

    private void advance() {
        current = lexer.next();
    }
}
