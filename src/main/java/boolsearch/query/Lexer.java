package boolsearch.query;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

/** 查询词法分析：识别 AND/OR/NOT（大小写不敏感，保留字）、括号与词项。 */
public final class Lexer {
    public enum Kind { AND, OR, NOT, LPAREN, RPAREN, TERM, EOF }

    public record Token(Kind kind, String text, int pos) {}

    private Lexer() {}

    public static List<Token> lex(String q) throws ParseException {
        List<Token> out = new ArrayList<>();
        int i = 0, n = q.length();
        while (i < n) {
            char c = q.charAt(i);
            if (Character.isWhitespace(c)) { i++; continue; }
            if (c == '(') { out.add(new Token(Kind.LPAREN, "(", i)); i++; continue; }
            if (c == ')') { out.add(new Token(Kind.RPAREN, ")", i)); i++; continue; }
            if (Character.isLetterOrDigit(c) || c == '_') {
                int j = i;
                while (j < n && (Character.isLetterOrDigit(q.charAt(j)) || q.charAt(j) == '_')) j++;
                String w = q.substring(i, j);
                Kind k = switch (w.toUpperCase(Locale.ROOT)) {
                    case "AND" -> Kind.AND;
                    case "OR" -> Kind.OR;
                    case "NOT" -> Kind.NOT;
                    default -> Kind.TERM;
                };
                // 词项统一小写，与文档分词保持一致
                out.add(new Token(k, k == Kind.TERM ? w.toLowerCase(Locale.ROOT) : w, i));
                i = j;
                continue;
            }
            throw new ParseException("非法字符 '" + c + "'", i);
        }
        out.add(new Token(Kind.EOF, "", n));
        return out;
    }
}
