package com.tvl.sql;

import com.tvl.sql.Lexer.Kind;
import com.tvl.sql.Lexer.Token;
import com.tvl.types.DataType;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.Set;

/**
 * 递归下降解析器，支持的子集：
 *
 *   SELECT * | 标量表达式 [AS] 别名 (, ...) FROM 表名 [WHERE 谓词]
 *   谓词（优先级由低到高）:
 *     or   := and (OR and)*
 *     and  := not (AND not)*
 *     not  := NOT not | atom
 *     atom := ( or )
 *           | 标量 比较 标量
 *           | 标量 IS [NOT] NULL
 *           | TRUE | FALSE
 *   标量 := 列名 | 数字 | '字符串' | ? | NULL
 *
 * 关键字大小写不敏感；列名大小写敏感。
 */
public final class Parser {

    private static final Set<String> COMPARISONS =
            Set.of("=", "<>", "!=", "<", "<=", ">", ">=");

    private final List<Token> tokens;
    private int index;
    private int paramCount;

    private Parser(List<Token> tokens) {
        this.tokens = tokens;
    }

    /** 解析结果：语句 + SQL 中出现的位置参数个数。 */
    public record Parsed(Select statement, int paramCount) {
    }

    public static Select parse(String sql) {
        return parseDetailed(sql).statement();
    }

    public static Parsed parseDetailed(String sql) {
        if (sql == null || sql.isBlank()) {
            throw new SqlParseException("SQL 为空");
        }
        Parser p = new Parser(Lexer.tokenize(sql));
        Select stmt = p.parseStatement();
        return new Parsed(stmt, p.paramCount);
    }

    private Select parseStatement() {
        expectKeyword("SELECT");
        List<Select.ProjectionItem> items = parseProjection();
        expectKeyword("FROM");
        Token table = expect(Kind.IDENT, "表名");
        Expr where = null;
        if (isKeyword("WHERE")) {
            consume();
            where = parseOr();
        }
        expect(Kind.EOF, "语句结束");
        return new Select(items, table.text, where);
    }

    private List<Select.ProjectionItem> parseProjection() {
        List<Select.ProjectionItem> items = new ArrayList<>();
        if (peek().kind == Kind.STAR) {
            consume();
            items.add(new Select.ProjectionItem(new Expr.Literal(DataType.INTEGER, 1L), "*"));
            return items;
        }
        do {
            Expr expr = parseScalar();
            String alias;
            if (isKeyword("AS")) {
                consume();
                alias = expect(Kind.IDENT, "别名").text;
            } else if (peek().kind == Kind.IDENT
                    && !isKeywordAt(peek(), "FROM") && !isKeywordAt(peek(), "WHERE")) {
                alias = consume().text;
            } else {
                alias = defaultAlias(expr);
            }
            items.add(new Select.ProjectionItem(expr, alias));
        } while (match(Kind.COMMA));
        return items;
    }

    /* ---------------- 谓词 ---------------- */

    private Expr parseOr() {
        Expr left = parseAnd();
        while (isKeyword("OR")) {
            consume();
            Expr right = parseAnd();
            left = new Expr.Or(left, right);
        }
        return left;
    }

    private Expr parseAnd() {
        Expr left = parseNot();
        while (isKeyword("AND")) {
            consume();
            Expr right = parseNot();
            left = new Expr.And(left, right);
        }
        return left;
    }

    private Expr parseNot() {
        if (isKeyword("NOT")) {
            consume();
            // NOT NOT a、NOT (a OR b) 都合法；NOT UNKNOWN = UNKNOWN 由求值器保证。
            return new Expr.Not(parseNot());
        }
        return parseAtom();
    }

    private Expr parseAtom() {
        if (match(Kind.LPAREN)) {
            Expr inner = parseOr();
            expect(Kind.RPAREN, "右括号 )");
            return inner;
        }
        if (isKeyword("TRUE")) {
            consume();
            return new Expr.BoolLit(true);
        }
        if (isKeyword("FALSE")) {
            consume();
            return new Expr.BoolLit(false);
        }

        Expr first = parseScalar();

        if (isKeyword("IS")) {
            consume();
            boolean negate = false;
            if (isKeyword("NOT")) {
                consume();
                negate = true;
            }
            expectKeyword("NULL");
            return new Expr.IsNull(first, negate);
        }

        if (peek().kind == Kind.OP && COMPARISONS.contains(peek().text)) {
            String op = normalizeOp(consume().text);
            Expr second = parseScalar();
            return new Expr.Comparison(op, first, second);
        }

        // 裸标量作谓词（BOOLEAN 列/参数）：是否合法由类型分析器决定；
        // 数值/字符串标量会在那里被拒绝。
        return first;
    }

    /* ---------------- 标量 ---------------- */

    private Expr parseScalar() {
        Token t = peek();
        switch (t.kind) {
            case PARAM:
                consume();
                return new Expr.Param(paramCount++);
            case NUMBER:
                consume();
                return parseNumberLiteral(t);
            case STRING:
                consume();
                return new Expr.Literal(DataType.STRING, t.text);
            case IDENT:
                if (isKeywordAt(t, "NULL")) {
                    consume();
                    return new Expr.Literal(null, null);
                }
                if (isKeywordAt(t, "TRUE")) {
                    consume();
                    return new Expr.BoolLit(true);
                }
                if (isKeywordAt(t, "FALSE")) {
                    consume();
                    return new Expr.BoolLit(false);
                }
                consume();
                return new Expr.ColumnRef(t.text);
            case LPAREN:
                throw new SqlParseException("标量位置不支持括号表达式（位置 " + t.pos + "）");
            default:
                throw new SqlParseException("需要标量表达式（列名/数字/字符串/?/NULL），实际遇到 "
                        + describe(t));
        }
    }

    private Expr parseNumberLiteral(Token t) {
        String text = t.text;
        try {
            if (text.indexOf('.') >= 0) {
                double d = Double.parseDouble(text);
                if (!Double.isFinite(d)) {
                    throw new SqlParseException("数字字面量超出有限范围: " + text);
                }
                return new Expr.Literal(DataType.DOUBLE, d);
            }
            long v = Long.parseLong(text);
            return new Expr.Literal(DataType.INTEGER, v);
        } catch (NumberFormatException e) {
            throw new SqlParseException("非法数字字面量: " + text);
        }
    }

    private static String normalizeOp(String op) {
        return "!=".equals(op) ? "<>" : op;
    }

    private String defaultAlias(Expr expr) {
        if (expr instanceof Expr.ColumnRef c) {
            return c.name();
        }
        if (expr instanceof Expr.Literal l) {
            if (l.value() == null) {
                return "NULL";
            }
            return l.type() == DataType.STRING ? "'" + l.value() + "'" : String.valueOf(l.value());
        }
        if (expr instanceof Expr.Param p) {
            return "?" + (p.index() + 1);
        }
        if (expr instanceof Expr.BoolLit b) {
            return b.value() ? "TRUE" : "FALSE";
        }
        return "expr";
    }

    /* ---------------- token 工具 ---------------- */

    private Token peek() {
        return tokens.get(index);
    }

    private Token consume() {
        return tokens.get(index++);
    }

    private boolean match(Kind kind) {
        if (peek().kind == kind) {
            consume();
            return true;
        }
        return false;
    }

    private Token expect(Kind kind, String what) {
        Token t = peek();
        if (t.kind != kind) {
            throw new SqlParseException("需要 " + what + "，实际遇到 " + describe(t));
        }
        return consume();
    }

    private void expectKeyword(String keyword) {
        Token t = peek();
        if (t.kind != Kind.IDENT || !isKeywordAt(t, keyword)) {
            throw new SqlParseException("需要关键字 " + keyword + "，实际遇到 " + describe(t));
        }
        consume();
    }

    private boolean isKeyword(String keyword) {
        return isKeywordAt(peek(), keyword);
    }

    private static boolean isKeywordAt(Token t, String keyword) {
        return t.kind == Kind.IDENT
                && t.text.toUpperCase(Locale.ROOT).equals(keyword.toUpperCase(Locale.ROOT));
    }

    private static String describe(Token t) {
        if (t.kind == Kind.EOF) {
            return "语句结束";
        }
        return "'" + t.text + "'（位置 " + t.pos + "）";
    }
}
