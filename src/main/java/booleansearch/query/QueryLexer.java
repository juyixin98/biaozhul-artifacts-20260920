package booleansearch.query;

import java.util.ArrayList;
import java.util.List;

/**
 * 查询词法分析器。
 *
 * <p>Token 类型：
 * <ul>
 *   <li>{@code AND / OR / NOT}：保留字（大小写不敏感），也接受 {@code & / | / - / !}</li>
 *   <li>{@code LPAREN / RPAREN}：括号，也接受中文全角括号（）</li>
 *   <li>{@code TERM}：字母数字组成的词项，统一转小写</li>
 * </ul>
 * 空白仅作分隔；其他字符（如引号、中文词、@）直接报错并保留位置。
 */
final class QueryLexer {

    enum Type { TERM, AND, OR, NOT, LPAREN, RPAREN, EOF }

    record Token(Type type, String text, int position) {
    }

    private final String input;
    private int pos = 0;

    private QueryLexer(String input) {
        this.input = input;
    }

    static List<Token> lex(String input) {
        return new QueryLexer(input).tokenize();
    }

    private List<Token> tokenize() {
        List<Token> tokens = new ArrayList<>();
        while (pos < input.length()) {
            char c = input.charAt(pos);
            if (Character.isWhitespace(c)) {
                pos++;
            } else if (c == '(' || c == '（') {
                tokens.add(new Token(Type.LPAREN, String.valueOf(c), pos++));
            } else if (c == ')' || c == '）') {
                tokens.add(new Token(Type.RPAREN, String.valueOf(c), pos++));
            } else if (c == '&') {
                tokens.add(new Token(Type.AND, "&", pos++));
            } else if (c == '|') {
                tokens.add(new Token(Type.OR, "|", pos++));
            } else if (c == '-' || c == '!') {
                tokens.add(new Token(Type.NOT, String.valueOf(c), pos++));
            } else if (isWordChar(c)) {
                readWord(tokens);
            } else {
                throw new QueryParseException("无法识别的字符 '" + c + "'", pos);
            }
        }
        tokens.add(new Token(Type.EOF, "", pos));
        return tokens;
    }

    private void readWord(List<Token> tokens) {
        int start = pos;
        while (pos < input.length() && isWordChar(input.charAt(pos))) {
            pos++;
        }
        String word = input.substring(start, pos).toLowerCase();
        Type type = switch (word) {
            case "and" -> Type.AND;
            case "or" -> Type.OR;
            case "not" -> Type.NOT;
            default -> Type.TERM;
        };
        tokens.add(new Token(type, word, start));
    }

    private static boolean isWordChar(char c) {
        // 只收 ASCII 字母数字与下划线；Java 的 Character.isLetterOrDigit 会把
        // 中文等 CJK 字符也算作字母，这里必须显式限制，否则中文会被静默吞掉而不是报错。
        return (c >= 'a' && c <= 'z')
                || (c >= 'A' && c <= 'Z')
                || (c >= '0' && c <= '9')
                || c == '_';
    }
}
