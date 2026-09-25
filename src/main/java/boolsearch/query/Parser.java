package boolsearch.query;

import java.util.ArrayList;
import java.util.List;

/**
 * 递归下降解析器。
 * 优先级：NOT > AND > OR；括号可覆盖。
 * 文法：
 *   orExpr  := andExpr (OR andExpr)*
 *   andExpr := notExpr (AND notExpr)*
 *   notExpr := NOT notExpr | primary
 *   primary := '(' orExpr ')' | TERM
 * 所有错误抛出 ParseException 并保留字符位置。
 */
public final class Parser {
    private final List<Lexer.Token> tokens;
    private int pos;

    private Parser(List<Lexer.Token> tokens) {
        this.tokens = tokens;
    }

    public static Node parse(String query) throws ParseException {
        Parser p = new Parser(Lexer.lex(query));
        Node n = p.parseOr();
        Lexer.Token t = p.peek();
        if (t.kind() != Lexer.Kind.EOF) {
            throw new ParseException("意外的记号 '" + t.text() + "'（缺少运算符？）", t.pos());
        }
        return n;
    }

    private Lexer.Token peek() { return tokens.get(pos); }
    private void advance() { pos++; }

    private Node parseOr() throws ParseException {
        List<Node> parts = new ArrayList<>();
        parts.add(parseAnd());
        while (peek().kind() == Lexer.Kind.OR) {
            advance();
            parts.add(parseAnd());
        }
        return parts.size() == 1 ? parts.get(0) : new Node.Or(parts);
    }

    private Node parseAnd() throws ParseException {
        List<Node> parts = new ArrayList<>();
        parts.add(parseNot());
        while (peek().kind() == Lexer.Kind.AND) {
            advance();
            parts.add(parseNot());
        }
        return parts.size() == 1 ? parts.get(0) : new Node.And(parts);
    }

    private Node parseNot() throws ParseException {
        if (peek().kind() == Lexer.Kind.NOT) {
            advance();
            return new Node.Not(parseNot());
        }
        return parsePrimary();
    }

    private Node parsePrimary() throws ParseException {
        Lexer.Token t = peek();
        switch (t.kind()) {
            case TERM -> {
                advance();
                return new Node.Term(t.text());
            }
            case LPAREN -> {
                advance();
                Node inner = parseOr();
                Lexer.Token close = peek();
                if (close.kind() != Lexer.Kind.RPAREN) {
                    throw new ParseException("期望 ')'，但遇到 " + describe(close), close.pos());
                }
                advance();
                return inner;
            }
            default -> throw new ParseException("期望词项或 '('，但遇到 " + describe(t), t.pos());
        }
    }

    private static String describe(Lexer.Token t) {
        return t.kind() == Lexer.Kind.EOF ? "输入结束" : "'" + t.text() + "'";
    }
}
