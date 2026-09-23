package ppd;

import java.util.ArrayList;
import java.util.List;

/**
 * 谓词表达式解析器（递归下降，无第三方依赖）。
 *
 * 文法（优先级从低到高）：
 *   or     := and ( OR and )*
 *   and    := not ( AND not )*
 *   not    := NOT not | cmp
 *   cmp    := add [ (= &lt;&gt; &lt; &lt;= &gt; &gt;= ) add | IS [NOT] NULL ]
 *   add    := mul ( (+ | -) mul )*
 *   mul    := atom ( * atom )*
 *   atom   := 字面量 | 列引用 | '(' or ')'
 *
 * 字面量：NULL/TRUE/FALSE、整数、小数、'单引号字符串'（'' 转义）。
 * 列引用：col 或 alias.col；可用双引号包裹标识符。
 */
public final class ExprParser {

    private final List<Token> tokens;
    private int pos;

    private ExprParser(List<Token> tokens) {
        this.tokens = tokens;
    }

    public static Expr parse(String text) {
        ExprParser p = new ExprParser(lex(text));
        Expr e = p.parseOr();
        if (!p.atEnd()) throw new EngineException("谓词存在多余 token: " + p.peek().text);
        return e;
    }

    // ------------------------------------------------------------------
    // 递归下降
    // ------------------------------------------------------------------

    private Expr parseOr() {
        Expr left = parseAnd();
        while (matchKeyword("OR")) {
            left = new Expr.Logic(Expr.LogicOp.OR, left, parseAnd());
        }
        return left;
    }

    private Expr parseAnd() {
        Expr left = parseNot();
        while (matchKeyword("AND")) {
            left = new Expr.Logic(Expr.LogicOp.AND, left, parseNot());
        }
        return left;
    }

    private Expr parseNot() {
        if (matchKeyword("NOT")) {
            return new Expr.Not(parseNot());
        }
        return parseCmp();
    }

    private Expr parseCmp() {
        Expr left = parseAdd();
        if (matchKeyword("IS")) {
            boolean negated = matchKeyword("NOT");
            requireKeyword("NULL");
            return new Expr.IsNull(left, negated);
        }
        if (peek().type == TokenType.OP) {
            String op = advance().text;
            Expr right = parseAdd();
            Expr.CmpOp cmp = switch (op) {
                case "=" -> Expr.CmpOp.EQ;
                case "<>" -> Expr.CmpOp.NE;
                case "!=" -> Expr.CmpOp.NE;
                case "<" -> Expr.CmpOp.LT;
                case "<=" -> Expr.CmpOp.LE;
                case ">" -> Expr.CmpOp.GT;
                case ">=" -> Expr.CmpOp.GE;
                default -> throw new EngineException("未知比较运算符: " + op);
            };
            return new Expr.Cmp(cmp, left, right);
        }
        return left;
    }

    private Expr parseAdd() {
        Expr left = parseMul();
        while (true) {
            if (peek().type == TokenType.OP && "+".equals(peek().text)) {
                advance();
                left = new Expr.Arith(Expr.ArithOp.ADD, left, parseMul());
            } else if (peek().type == TokenType.MINUS) {
                advance();
                left = new Expr.Arith(Expr.ArithOp.SUB, left, parseMul());
            } else {
                return left;
            }
        }
    }

    private Expr parseMul() {
        Expr left = parseAtom();
        while (peek().type == TokenType.OP && "*".equals(peek().text)) {
            advance();
            left = new Expr.Arith(Expr.ArithOp.MUL, left, parseAtom());
        }
        return left;
    }

    private Expr parseAtom() {
        Token t = peek();
        if (t.type == TokenType.LPAREN) {
            advance();
            Expr e = parseOr();
            Token close = advance();
            if (close.type != TokenType.RPAREN) throw new EngineException("缺少右括号");
            return e;
        }
        if (t.type == TokenType.STRING) {
            advance();
            return new Expr.Lit(t.text);
        }
        if (t.type == TokenType.NUMBER) {
            advance();
            Object v;
            if (t.text.contains(".")) v = Double.valueOf(t.text);
            else v = Long.valueOf(t.text);
            return new Expr.Lit(v);
        }        if (t.type == TokenType.IDENT) {
            advance();
            String upper = t.text.toUpperCase();
            if (upper.equals("NULL")) return new Expr.Lit(null);
            if (upper.equals("TRUE")) return new Expr.Lit(Boolean.TRUE);
            if (upper.equals("FALSE")) return new Expr.Lit(Boolean.FALSE);
            String first = t.text;
            // 点号分隔的限定列
            if (peek().type == TokenType.DOT) {
                advance();
                Token col = advance();
                if (col.type != TokenType.IDENT) throw new EngineException("'.' 后期望列名");
                return new Expr.Ref(first, col.text);
            }
            return new Expr.Ref(null, first);
        }
        throw new EngineException("无法解析的 token: " + t.text);
    }

    // ------------------------------------------------------------------
    // token 工具
    // ------------------------------------------------------------------

    private Token peek() { return tokens.get(pos); }

    private Token advance() {
        Token t = tokens.get(pos);
        if (tokens.get(pos).type != TokenType.EOF) pos++;
        return t;
    }

    private boolean atEnd() {
        Token t = tokens.get(pos);
        return t.type == TokenType.EOF;
    }

    private boolean matchKeyword(String kw) {
        Token t = peek();
        if (t.type == TokenType.IDENT && t.text.equalsIgnoreCase(kw)) {
            advance();
            return true;
        }
        return false;
    }

    private void requireKeyword(String kw) {
        if (!matchKeyword(kw)) {
            throw new EngineException("期望关键字 " + kw + "，实际为 " + peek().text);
        }
    }

    // ------------------------------------------------------------------
    // 词法
    // ------------------------------------------------------------------

    private enum TokenType { IDENT, NUMBER, STRING, OP, MINUS, DOT, LPAREN, RPAREN, EOF }

    private record Token(TokenType type, String text) {}

    private static List<Token> lex(String s) {
        List<Token> out = new ArrayList<>();
        int i = 0;
        int n = s.length();
        while (i < n) {
            char c = s.charAt(i);
            if (Character.isWhitespace(c)) { i++; continue; }
            switch (c) {
                case '(' -> { out.add(new Token(TokenType.LPAREN, "(")); i++; }
                case ')' -> { out.add(new Token(TokenType.RPAREN, ")")); i++; }
                case '.' -> { out.add(new Token(TokenType.DOT, ".")); i++; }
                case '*' -> { out.add(new Token(TokenType.OP, "*")); i++; }
                case '+' -> { out.add(new Token(TokenType.OP, "+")); i++; }
                case '-' -> {
                    if (i + 1 < n && s.charAt(i + 1) == '-') { i += 2; continue; }
                    out.add(new Token(TokenType.MINUS, "-"));
                    i++;
                }
                case '=', '<', '>', '!' -> {
                    if (i + 1 < n && (s.charAt(i + 1) == '=' || s.charAt(i + 1) == '>')) {
                        out.add(new Token(TokenType.OP, s.substring(i, i + 2)));
                        i += 2;
                    } else {
                        out.add(new Token(TokenType.OP, String.valueOf(c)));
                        i++;
                    }
                }
                case '\'' -> {
                    StringBuilder sb = new StringBuilder();
                    i++;
                    while (true) {
                        if (i >= n) throw new EngineException("字符串未闭合");
                        char ch = s.charAt(i);
                        if (ch == '\'') {
                            if (i + 1 < n && s.charAt(i + 1) == '\'') {
                                sb.append('\'');
                                i += 2;
                            } else {
                                i++;
                                break;
                            }
                        } else {
                            sb.append(ch);
                            i++;
                        }
                    }
                    out.add(new Token(TokenType.STRING, sb.toString()));
                }
                case '"' -> {
                    int end = s.indexOf('"', i + 1);
                    if (end < 0) throw new EngineException("标识符未闭合");
                    out.add(new Token(TokenType.IDENT, s.substring(i + 1, end)));
                    i = end + 1;
                }
                default -> {
                    if (Character.isDigit(c)) {
                        int start = i;
                        while (i < n && Character.isDigit(s.charAt(i))) i++;
                        if (i < n && s.charAt(i) == '.') {
                            i++;
                            while (i < n && Character.isDigit(s.charAt(i))) i++;
                        }
                        out.add(new Token(TokenType.NUMBER, s.substring(start, i)));
                    } else if (isIdentStart(c)) {
                        int start = i;
                        while (i < n && isIdentPart(s.charAt(i))) i++;
                        out.add(new Token(TokenType.IDENT, s.substring(start, i)));
                    } else {
                        throw new EngineException("非法字符: " + c);
                    }
                }
            }
        }
        out.add(new Token(TokenType.EOF, ""));
        return out;
    }

    private static boolean isIdentStart(char c) {
        return Character.isLetter(c) || c == '_';
    }

    private static boolean isIdentPart(char c) {
        return Character.isLetterOrDigit(c) || c == '_';
    }
}
