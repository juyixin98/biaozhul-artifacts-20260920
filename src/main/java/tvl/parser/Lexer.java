package tvl.parser;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

import tvl.core.CompileException;
import tvl.core.Pos;

/**
 * 词法分析器：把表达式源码切分为 Token 流。
 *
 * 支持：
 *  - 空白与 SQL 风格注释（-- 行注释、&#47;* *&#47; 块注释）
 *  - 整数字面量（仅十进制）、单引号字符串（'' 转义单引号）
 *  - 标识符（字母/下划线开头），关键字大小写不敏感
 *  - 算术与比较操作符、括号
 */
public final class Lexer {

    private static final Map<String, TokenType> KEYWORDS = new HashMap<>();
    static {
        KEYWORDS.put("AND", TokenType.AND);
        KEYWORDS.put("OR", TokenType.OR);
        KEYWORDS.put("NOT", TokenType.NOT);
        KEYWORDS.put("IS", TokenType.IS);
        KEYWORDS.put("TRUE", TokenType.TRUE);
        KEYWORDS.put("FALSE", TokenType.FALSE);
        KEYWORDS.put("NULL", TokenType.NULL_KW);
    }

    private final String src;
    private int pos = 0;
    private int line = 1;
    private int col = 1;
    private final List<Token> tokens = new ArrayList<>();

    public Lexer(String src) {
        this.src = src == null ? "" : src;
    }

    public List<Token> tokenize() {
        while (pos < src.length()) {
            char c = src.charAt(pos);
            if (isWhitespace(c)) {
                advance();
            } else if (c == '-' && peek(1) == '-') {
                skipLineComment();
            } else if (c == '/' && peek(1) == '*') {
                skipBlockComment();
            } else if (Character.isDigit(c)) {
                readNumber();
            } else if (c == '\'') {
                readString();
            } else if (isIdentStart(c)) {
                readIdentOrKeyword();
            } else {
                readOperator();
            }
        }
        tokens.add(new Token(TokenType.EOF, "", new Pos(pos, line, col), pos));
        return tokens;
    }

    private void skipLineComment() {
        advance(); // -
        advance(); // -
        while (pos < src.length() && src.charAt(pos) != '\n') {
            advance();
        }
    }

    private void skipBlockComment() {
        Pos start = here();
        advance(); // /
        advance(); // *
        while (pos < src.length()) {
            if (src.charAt(pos) == '*' && peek(1) == '/') {
                advance();
                advance();
                return;
            }
            advance();
        }
        throw new CompileException("UNTERMINATED_COMMENT",
                "块注释缺少结束标记 */", start, 2);
    }

    private void readNumber() {
        Pos start = here();
        int begin = pos;
        while (pos < src.length() && Character.isDigit(src.charAt(pos))) {
            advance();
        }
        String text = src.substring(begin, pos);
        tokens.add(new Token(TokenType.INTEGER, text, start, pos));
    }

    private void readString() {
        Pos start = here();
        advance(); // 开引号
        StringBuilder sb = new StringBuilder();
        while (true) {
            if (pos >= src.length()) {
                throw new CompileException("UNTERMINATED_STRING",
                        "字符串字面量缺少结束单引号 '", start, 1);
            }
            char c = src.charAt(pos);
            if (c == '\'') {
                if (peek(1) == '\'') {
                    sb.append('\'');
                    advance();
                    advance();
                } else {
                    advance(); // 闭引号
                    break;
                }
            } else {
                sb.append(c);
                advance();
            }
        }
        tokens.add(new Token(TokenType.STRING, sb.toString(), start, pos));
    }

    private void readIdentOrKeyword() {
        Pos start = here();
        int begin = pos;
        while (pos < src.length() && isIdentPart(src.charAt(pos))) {
            advance();
        }
        String text = src.substring(begin, pos);
        TokenType kw = KEYWORDS.get(text.toUpperCase());
        if (kw != null) {
            tokens.add(new Token(kw, text, start, pos));
        } else {
            tokens.add(new Token(TokenType.IDENT, text, start, pos));
        }
    }

    private void readOperator() {
        Pos start = here();
        char c = src.charAt(pos);
        char n = peek(1);
        switch (c) {
            case '+': single(TokenType.PLUS, "+"); return;
            case '-': single(TokenType.MINUS, "-"); return;
            case '*': single(TokenType.STAR, "*"); return;
            case '/': single(TokenType.SLASH, "/"); return;
            case '(': single(TokenType.LPAREN, "("); return;
            case ')': single(TokenType.RPAREN, ")"); return;
            case '=': single(TokenType.EQ, "="); return;
            case '<':
                if (n == '=') { twoChar(TokenType.LE, "<="); }
                else if (n == '>') { twoChar(TokenType.NE, "<>"); }
                else { single(TokenType.LT, "<"); }
                return;
            case '>':
                if (n == '=') { twoChar(TokenType.GE, ">="); }
                else { single(TokenType.GT, ">"); }
                return;
            case '!':
                if (n == '=') {
                    twoChar(TokenType.NE, "!=");
                    return;
                }
                break;
            default:
                break;
        }
        throw new CompileException("UNEXPECTED_CHAR",
                "无法识别的字符 '" + c + "'（U+" + String.format("%04X", (int) c) + "）",
                start, 1);
    }

    private void single(TokenType type, String text) {
        Pos start = here();
        advance();
        tokens.add(new Token(type, text, start, pos));
    }

    private void twoChar(TokenType type, String text) {
        Pos start = here();
        advance();
        advance();
        tokens.add(new Token(type, text, start, pos));
    }

    private boolean isWhitespace(char c) {
        return c == ' ' || c == '\t' || c == '\r' || c == '\n' || c == '\f';
    }

    private boolean isIdentStart(char c) {
        return Character.isLetter(c) || c == '_';
    }

    private boolean isIdentPart(char c) {
        return Character.isLetterOrDigit(c) || c == '_';
    }

    private Pos here() {
        return new Pos(pos, line, col);
    }

    private char peek(int ahead) {
        int p = pos + ahead;
        return p < src.length() ? src.charAt(p) : '\0';
    }

    private void advance() {
        if (pos < src.length()) {
            if (src.charAt(pos) == '\n') {
                line++;
                col = 1;
            } else {
                col++;
            }
            pos++;
        }
    }
}
