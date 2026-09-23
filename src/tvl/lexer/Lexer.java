package tvl.lexer;

import java.util.Map;

/**
 * 表达式词法分析器。
 *
 * 支持：
 *   - 64 位整数字面量（溢出抛 {@link LexException}）
 *   - 单引号字符串字面量，'' 表示一个单引号（SQL 风格）
 *   - 标识符/关键字（关键字大小写不敏感）
 *   - 比较符 = == <> != &lt; &lt;= &gt; &gt;=，算术 + - * /，括号
 *
 * 空白（空格、制表符、换行）被跳过，行号列号随源码保留以便定位错误。
 */
public final class Lexer {

    private static final Map<String, TokenType> KEYWORDS = Map.of(
            "and", TokenType.AND,
            "or", TokenType.OR,
            "not", TokenType.NOT,
            "is", TokenType.IS,
            "null", TokenType.NULL,
            "true", TokenType.TRUE,
            "false", TokenType.FALSE,
            "unknown", TokenType.UNKNOWN
    );

    private final String src;
    private int pos;
    private int line = 1;
    private int column = 1;

    public Lexer(String src) {
        this.src = src;
    }

    public Token next() {
        skipWhitespaceAndComments();
        if (pos >= src.length()) {
            return new Token(TokenType.EOF, "", pos, line, column);
        }
        int startPos = pos;
        int startLine = line;
        int startColumn = column;
        char c = src.charAt(pos);

        if (Character.isDigit(c)) {
            return readNumber(startPos, startLine, startColumn);
        }
        if (isIdentStart(c)) {
            return readIdentifier(startPos, startLine, startColumn);
        }
        if (c == '\'') {
            return readString(startPos, startLine, startColumn);
        }
        return readOperator(startPos, startLine, startColumn);
    }

    // ---------------------------------------------------------------------

    private void skipWhitespaceAndComments() {
        while (pos < src.length()) {
            char c = src.charAt(pos);
            if (c == ' ' || c == '\t' || c == '\r') {
                advance();
            } else if (c == '\n') {
                advance(); // advance() 内部更新行号
            } else {
                break;
            }
        }
    }

    private Token readNumber(int startPos, int startLine, int startColumn) {
        StringBuilder digits = new StringBuilder();
        while (pos < src.length() && Character.isDigit(src.charAt(pos))) {
            digits.append(src.charAt(pos));
            advance();
        }
        String token = digits.toString();
        long value;
        if (token.equals(MIN_LONG_DIGITS)) {
            // 唯一一个不能写成正数、但在一元负号后合法的字面量：-9223372036854775808
            // 交给 Parser.parseUnary 处理；不带负号直接出现时在解析期报错。
            return new Token(TokenType.INTEGER, token, 0L, startPos, startLine, startColumn);
        }
        try {
            value = Long.parseLong(token);
        } catch (NumberFormatException ex) {
            throw new LexException(
                    "integer literal '" + token + "' is out of 64-bit range", startPos);
        }
        return new Token(TokenType.INTEGER, token, value, startPos, startLine, startColumn);
    }

    /** 9223372036854775808 = |Long.MIN_VALUE|，仅允许出现在一元负号之后。 */
    public static final String MIN_LONG_DIGITS = "9223372036854775808";

    private Token readIdentifier(int startPos, int startLine, int startColumn) {
        StringBuilder sb = new StringBuilder();
        while (pos < src.length() && isIdentPart(src.charAt(pos))) {
            sb.append(src.charAt(pos));
            advance();
        }
        String word = sb.toString();
        TokenType kw = KEYWORDS.get(word.toLowerCase());
        if (kw != null) {
            return new Token(kw, word, startPos, startLine, startColumn);
        }
        return new Token(TokenType.IDENT, word, startPos, startLine, startColumn);
    }

    private Token readString(int startPos, int startLine, int startColumn) {
        advance(); // 跳过起始引号
        StringBuilder sb = new StringBuilder();
        while (true) {
            if (pos >= src.length()) {
                throw new LexException("unterminated string literal (missing closing ') ", startPos);
            }
            char c = src.charAt(pos);
            if (c == '\'') {
                advance();
                // SQL 风格：连续两个单引号表示一个单引号
                if (pos < src.length() && src.charAt(pos) == '\'') {
                    sb.append('\'');
                    advance();
                } else {
                    break;
                }
            } else {
                sb.append(c);
                advance();
            }
        }
        return new Token(TokenType.STRING, sb.toString(), startPos, startLine, startColumn);
    }

    private Token readOperator(int startPos, int startLine, int startColumn) {
        char c = src.charAt(pos);
        switch (c) {
            case '(' -> {
                advance();
                return token(TokenType.LPAREN, "(", startPos, startLine, startColumn);
            }
            case ')' -> {
                advance();
                return token(TokenType.RPAREN, ")", startPos, startLine, startColumn);
            }
            case '+' -> {
                advance();
                return token(TokenType.PLUS, "+", startPos, startLine, startColumn);
            }
            case '-' -> {
                advance();
                return token(TokenType.MINUS, "-", startPos, startLine, startColumn);
            }
            case '*' -> {
                advance();
                return token(TokenType.STAR, "*", startPos, startLine, startColumn);
            }
            case '/' -> {
                advance();
                return token(TokenType.SLASH, "/", startPos, startLine, startColumn);
            }
            case '=' -> {
                advance();
                consumeIf('='); // 容忍 SQL 系之外的 == 写法
                return token(TokenType.EQ, "=", startPos, startLine, startColumn);
            }
            case '<' -> {
                advance();
                if (consumeIf('=')) return token(TokenType.LE, "<=", startPos, startLine, startColumn);
                if (consumeIf('>')) return token(TokenType.NE, "<>", startPos, startLine, startColumn);
                return token(TokenType.LT, "<", startPos, startLine, startColumn);
            }
            case '>' -> {
                advance();
                if (consumeIf('=')) return token(TokenType.GE, ">=", startPos, startLine, startColumn);
                return token(TokenType.GT, ">", startPos, startLine, startColumn);
            }
            case '!' -> {
                advance();
                if (consumeIf('=')) return token(TokenType.NE, "!=", startPos, startLine, startColumn);
                throw new LexException("unexpected character '!'; did you mean '!=' ?", startPos);
            }
            default -> throw new LexException("unexpected character '" + c + "'", startPos);
        }
    }

    private Token token(TokenType type, String text, int pos, int line, int column) {
        return new Token(type, text, pos, line, column);
    }

    private boolean consumeIf(char c) {
        if (pos < src.length() && src.charAt(pos) == c) {
            advance();
            return true;
        }
        return false;
    }

    private void advance() {
        if (pos < src.length()) {
            if (src.charAt(pos) == '\n') {
                line++;
                column = 1;
            } else {
                column++;
            }
            pos++;
        }
    }

    private static boolean isIdentStart(char c) {
        return Character.isLetter(c) || c == '_';
    }

    private static boolean isIdentPart(char c) {
        return Character.isLetterOrDigit(c) || c == '_';
    }
}
