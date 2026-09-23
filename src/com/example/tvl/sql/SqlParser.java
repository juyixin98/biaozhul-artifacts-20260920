package com.example.tvl.sql;

import com.example.tvl.sql.Ast.BoolExpr;
import com.example.tvl.sql.Ast.ColumnRef;
import com.example.tvl.sql.Ast.Comparison;
import com.example.tvl.sql.Ast.Expr;
import com.example.tvl.sql.Ast.IsNull;
import com.example.tvl.sql.Ast.Literal;
import com.example.tvl.sql.Ast.Operand;
import com.example.tvl.sql.Ast.Param;
import com.example.tvl.sql.Ast.Query;
import com.example.tvl.sql.Tokenizer.Token;

import java.util.ArrayList;
import java.util.List;

/**
 * 递归下降解析器。文法见 {@link Ast} 类注释。
 *
 * 参数占位符按从左到右的出现顺序编号，从 0 开始。
 */
public final class SqlParser {

    private final List<Token> tokens;
    private int cursor;
    private int paramCount;

    private SqlParser(String sql) {
        this.tokens = new Tokenizer(sql).tokenize();
        this.cursor = 0;
        this.paramCount = 0;
    }

    public static Query parse(String sql) {
        if (sql == null || sql.isBlank()) {
            throw new SqlParseException("SQL 为空");
        }
        return new SqlParser(sql).parseQuery();
    }

    private Token peek() {
        return tokens.get(cursor);
    }

    private Token advance() {
        return tokens.get(cursor++);
    }

    private boolean check(String kind, String text) {
        Token t = peek();
        return t.kind().equals(kind) && t.text().equals(text);
    }

    private Token expect(String kind, String text) {
        Token t = peek();
        if (!t.kind().equals(kind) || (text != null && !t.text().equals(text))) {
            throw new SqlParseException("期望 " + describe(kind, text) + "，实际为 '" + t.text() + "'，位置 " + t.pos());
        }
        return advance();
    }

    private static String describe(String kind, String text) {
        return text != null ? "'" + text + "'" : kind;
    }

    private Query parseQuery() {
        expect("KEYWORD", "SELECT");
        boolean selectAll;
        List<String> columns = new ArrayList<>();
        if (check("SYMBOL", "*")) {
            advance();
            selectAll = true;
        } else {
            selectAll = false;
            columns.add(parseColumnName());
            while (check("SYMBOL", ",")) {
                advance();
                columns.add(parseColumnName());
            }
        }
        expect("KEYWORD", "FROM");
        Token tableToken = expect("IDENT", null);

        Expr where = null;
        if (peek().isKeyword("WHERE")) {
            advance();
            where = parseOr();
        }
        if (check("SYMBOL", ";")) {
            advance();
        }
        if (!peek().kind().equals("EOF")) {
            Token t = peek();
            throw new SqlParseException("WHERE 子句后存在无法识别的内容 '" + t.text() + "'，位置 " + t.pos());
        }
        return new Query(selectAll, List.copyOf(columns), tableToken.text(), where);
    }

    /** 暴露给外部的参数计数：重新解析一次并读取占位符数量。 */
    public static int paramCount(String sql) {
        SqlParser p = new SqlParser(sql);
        p.parseQuery();
        return p.paramCount;
    }

    private String parseColumnName() {
        Token t = expect("IDENT", null);
        return t.text();
    }

    // or := and (OR and)*
    private Expr parseOr() {
        List<Expr> terms = new ArrayList<>();
        terms.add(parseAnd());
        while (peek().isKeyword("OR")) {
            advance();
            terms.add(parseAnd());
        }
        return terms.size() == 1 ? terms.get(0) : new BoolExpr(false, List.copyOf(terms));
    }

    // and := not (AND not)*
    private Expr parseAnd() {
        List<Expr> terms = new ArrayList<>();
        terms.add(parseNot());
        while (peek().isKeyword("AND")) {
            advance();
            terms.add(parseNot());
        }
        return terms.size() == 1 ? terms.get(0) : new BoolExpr(true, List.copyOf(terms));
    }

    // not := NOT not | predicate
    private Expr parseNot() {
        if (peek().isKeyword("NOT")) {
            advance();
            return new Ast.NotExpr(parseNot());
        }
        return parsePredicate();
    }

    // predicate := '(' or ')' | comparison/is-null
    private Expr parsePredicate() {
        if (check("SYMBOL", "(")) {
            advance();
            Expr inner = parseOr();
            expect("SYMBOL", ")");
            return parseOptionalIsNull(inner);
        }
        Operand left = parseOperand();
        Token t = peek();

        // IS [NOT] NULL
        if (t.isKeyword("IS")) {
            advance();
            boolean negated = false;
            if (peek().isKeyword("NOT")) {
                advance();
                negated = true;
            }
            expect("KEYWORD", "NULL");
            return new IsNull(left, negated);
        }
        if (t.isKeyword("NOT")) {
            // 拒绝 "x NOT NULL" 这种缺 IS 的写法，避免与 NOT 逻辑取反混淆
            Token not = advance();
            throw new SqlParseException("期望 IS NULL 或比较运算符，位置 " + not.pos());
        }

        // 比较
        String op;
        if (t.kind().equals("SYMBOL") && List.of("=", "<>", "<", "<=", ">", ">=").contains(t.text())) {
            advance();
            op = t.text();
        } else {
            throw new SqlParseException("期望比较运算符或 IS NULL，实际为 '" + t.text() + "'，位置 " + t.pos());
        }
        Operand right = parseOperand();
        return parseOptionalIsNull(new Comparison(left, op, right));
    }

    /**
     * 不支持对复合谓词再做 IS NULL（布尔类型不存在）。
     * 括号表达式后若紧跟 IS NULL 在此报错。
     */
    private Expr parseOptionalIsNull(Expr already) {
        if (peek().isKeyword("IS")) {
            Token is = peek();
            throw new SqlParseException("IS NULL 只能作用于列/字面量/参数，不能作用于布尔表达式，位置 " + is.pos());
        }
        return already;
    }

    private Operand parseOperand() {
        Token t = peek();
        if (t.kind().equals("IDENT")) {
            advance();
            return new ColumnRef(t.text());
        }
        if (t.kind().equals("NUMBER")) {
            advance();
            Number n = (Number) t.value();
            DataType type = n instanceof Long ? DataType.INTEGER : DataType.FLOAT;
            return new Literal(n, type);
        }
        if (t.kind().equals("STRING")) {
            advance();
            return new Literal(t.value() == null ? "" : t.value().toString(), DataType.TEXT);
        }
        if (t.kind().equals("KEYWORD") && t.text().equals("NULL")) {
            advance();
            return new Literal(null, null);
        }
        if (check("SYMBOL", "?")) {
            advance();
            return new Param(paramCount++);
        }
        throw new SqlParseException("期望列名、字面量或 ? 参数，实际为 '" + t.text() + "'，位置 " + t.pos());
    }
}
