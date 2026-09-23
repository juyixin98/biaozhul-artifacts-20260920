package tvl.parser;

import java.util.List;

import tvl.core.CompileException;
import tvl.core.Pos;

/**
 * 递归下降解析器，产生表达式 AST。
 *
 * 优先级从低到高：
 * <pre>
 * orExpr    := andExpr (OR andExpr)*
 * andExpr   := notExpr (AND notExpr)*
 * notExpr   := NOT notExpr | isNullExpr
 * isNullExpr := compareExpr (IS [NOT] NULL)?
 * compareExpr := additiveExpr (compareOp additiveExpr)?   // 比较不允许连写
 * additiveExpr := mulExpr ((+|-) mulExpr)*
 * mulExpr   := unaryExpr ((*|/) unaryExpr)*
 * unaryExpr := - unaryExpr | primary
 * primary   := 整数 | 字符串 | TRUE | FALSE | NULL | 列名 | '(' orExpr ')'
 * </pre>
 *
 * 比较运算符为非结合：{@code a = b = c} 会在第二个 = 处报错。
 */
public final class Parser {

    private final List<Token> tokens;
    private int cur = 0;

    public Parser(List<Token> tokens) {
        this.tokens = tokens;
    }

    public static Expr parse(String source) {
        List<Token> toks = new Lexer(source).tokenize();
        Parser p = new Parser(toks);
        Expr e = p.parseOr();
        Token t = p.peek();
        if (t.type != TokenType.EOF) {
            throw p.error("SYNTAX_ERROR",
                    "表达式结束后仍有多余内容：'" + t.text + "'", t);
        }
        return e;
    }

    // ---------- 各优先级层级 ----------

    private Expr parseOr() {
        Expr left = parseAnd();
        while (match(TokenType.OR)) {
            Token op = last();
            Expr right = parseAnd();
            left = new BinaryExpr(TokenType.OR, op.text, left, right,
                    left.pos(), op.start, op.endOffset - op.start.offset,
                    endOffsetOf(right));
        }
        return left;
    }

    private Expr parseAnd() {
        Expr left = parseNot();
        while (match(TokenType.AND)) {
            Token op = last();
            Expr right = parseNot();
            left = new BinaryExpr(TokenType.AND, op.text, left, right,
                    left.pos(), op.start, op.endOffset - op.start.offset,
                    endOffsetOf(right));
        }
        return left;
    }

    private Expr parseNot() {
        if (match(TokenType.NOT)) {
            Token not = last();
            Expr operand = parseNot();
            return new NotExpr(operand, not.start, endOffsetOf(operand));
        }
        return parseIsNull();
    }

    private Expr parseIsNull() {
        Expr operand = parseComparison();
        if (match(TokenType.IS)) {
            Token is = last();
            boolean negated = match(TokenType.NOT);
            Token nullTok = expect(TokenType.NULL_KW,
                    "IS [NOT] 之后必须为 NULL");
            int end = nullTok.endOffset;
            return new IsNullExpr(operand, negated, operand.pos(), end);
        }
        return operand;
    }

    private Expr parseComparison() {
        Expr left = parseAdditive();
        if (isCompareOp(peek().type)) {
            Token op = advance();
            Expr right = parseAdditive();
            // 比较运算符非结合：右操作数解析完后若又出现比较符，报错
            // （例如 a = b = c、a < b > c；请用括号明确含义）
            if (isCompareOp(peek().type)) {
                throw error("NON_ASSOCIATIVE_COMPARISON",
                        "比较运算符不能连续使用（第二个运算符 '" + peek().text
                                + "'），请用括号明确结合方式", peek());
            }
            return new BinaryExpr(op.type, op.text, left, right,
                    left.pos(), op.start, op.endOffset - op.start.offset,
                    endOffsetOf(right));
        }
        return left;
    }

    private Expr parseAdditive() {
        Expr left = parseMul();
        while (peek().type == TokenType.PLUS || peek().type == TokenType.MINUS) {
            Token op = advance();
            Expr right = parseMul();
            left = new BinaryExpr(op.type, op.text, left, right,
                    left.pos(), op.start, op.endOffset - op.start.offset,
                    endOffsetOf(right));
        }
        return left;
    }

    private Expr parseMul() {
        Expr left = parseUnary();
        while (peek().type == TokenType.STAR || peek().type == TokenType.SLASH) {
            Token op = advance();
            Expr right = parseUnary();
            left = new BinaryExpr(op.type, op.text, left, right,
                    left.pos(), op.start, op.endOffset - op.start.offset,
                    endOffsetOf(right));
        }
        return left;
    }

    private Expr parseUnary() {
        if (match(TokenType.MINUS)) {
            Token minus = last();
            Expr operand = parseUnary();
            return new UnaryMinusExpr(operand, minus.start, endOffsetOf(operand));
        }
        return parsePrimary();
    }

    private Expr parsePrimary() {
        Token t = peek();
        switch (t.type) {
            case INTEGER: {
                advance();
                long v;
                try {
                    v = Long.parseLong(t.text);
                } catch (NumberFormatException ex) {
                    throw new CompileException("INTEGER_OUT_OF_RANGE",
                            "整数字面量超出 64 位有符号整数范围（"
                                    + Long.MIN_VALUE + " ~ " + Long.MAX_VALUE + "）",
                            t.start, t.endOffset - t.start.offset);
                }
                return new LiteralExpr(LiteralExpr.Kind.INTEGER, t.text, v,
                        t.start, t.endOffset - t.start.offset);
            }
            case STRING:
                advance();
                return new LiteralExpr(LiteralExpr.Kind.STRING, t.text, t.text,
                        t.start, t.endOffset - t.start.offset);
            case TRUE:
            case FALSE:
                advance();
                return new LiteralExpr(LiteralExpr.Kind.BOOLEAN, t.text,
                        t.type == TokenType.TRUE,
                        t.start, t.endOffset - t.start.offset);
            case NULL_KW:
                advance();
                return new LiteralExpr(LiteralExpr.Kind.NULL, t.text, null,
                        t.start, t.endOffset - t.start.offset);
            case IDENT:
                advance();
                return new ColumnExpr(t.text, t.start,
                        t.endOffset - t.start.offset);
            case LPAREN: {
                advance();
                Expr inner = parseOr();
                expect(TokenType.RPAREN, "缺少右括号 ')'");
                // 括号本身不产生 AST 节点；但节点位置只覆盖内部表达式，
                // 这不影响类型检查与求值。
                return inner;
            }
            case EOF:
                throw error("SYNTAX_ERROR", "表达式不完整：此处应有一个操作数", t);
            default:
                throw error("SYNTAX_ERROR",
                        "此处不应出现 '" + t.text + "'，应为操作数（数字、字符串、列名、括号表达式）",
                        t);
        }
    }

    // ---------- Token 游标工具 ----------

    private Token peek() {
        return tokens.get(cur);
    }

    private Token last() {
        return tokens.get(cur - 1);
    }

    private Token advance() {
        Token t = tokens.get(cur);
        if (cur < tokens.size() - 1) {
            cur++;
        }
        return t;
    }

    private boolean match(TokenType type) {
        if (peek().type == type) {
            advance();
            return true;
        }
        return false;
    }

    private Token expect(TokenType type, String message) {
        if (peek().type != type) {
            throw error("SYNTAX_ERROR", message + "，实际遇到 '" + peek().text + "'", peek());
        }
        return advance();
    }

    private boolean isCompareOp(TokenType t) {
        return t == TokenType.EQ || t == TokenType.NE
                || t == TokenType.LT || t == TokenType.LE
                || t == TokenType.GT || t == TokenType.GE;
    }

    private int endOffsetOf(Expr e) {
        return e.pos().offset + e.length();
    }

    private CompileException error(String code, String msg, Token t) {
        int len = Math.max(1, t.endOffset - t.start.offset);
        Pos pos = t.type == TokenType.EOF ? t.start : t.start;
        return new CompileException(code, msg, pos, len);
    }
}
