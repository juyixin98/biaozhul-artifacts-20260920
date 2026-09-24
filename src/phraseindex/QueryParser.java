package phraseindex;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

/**
 * Parses the query language into a {@link Query} AST.
 *
 * Grammar (implicit AND is supported between adjacent atoms):
 * <pre>
 * orExpr  := andExpr (OR andExpr)*
 * andExpr := unary [(AND | &lt;implicit&gt;) unary]*
 * unary   := NOT unary | atom
 * atom    := phrase | '(' orExpr ')'
 * phrase  := "whitespace separated words" | bareword
 * </pre>
 *
 * Keywords {@code AND}, {@code OR}, {@code NOT} are case-insensitive.
 * To search for a literal content word that equals a keyword, quote it:
 * {@code "and"}.
 */
public final class QueryParser {

    private enum TokenType {
        LPAREN, RPAREN, AND, OR, NOT, WORD, PHRASE, END
    }

    private record Token(TokenType type, String text, List<String> terms, int pos) {
    }

    private final List<Token> tokens;
    private int index;

    private QueryParser(String s) {
        this.tokens = lex(s);
    }

    public static Query parse(String s) {
        QueryParser p = new QueryParser(s);
        Query q = p.parseOr();
        Token t = p.peek();
        if (t.type != TokenType.END) {
            throw new QueryParseException("unexpected trailing input", t.pos);
        }
        return q;
    }

    private Query parseOr() {
        Query left = parseAnd();
        while (peek().type == TokenType.OR) {
            consume();
            Query right = parseAnd();
            left = new Query.Or(left, right);
        }
        return left;
    }

    private Query parseAnd() {
        Query left = parseUnary();
        while (true) {
            Token t = peek();
            switch (t.type) {
                case AND -> consume();
                case NOT, WORD, PHRASE, LPAREN -> {
                    // implicit AND: no keyword between the operands
                }
                default -> {
                    return left;
                }
            }
            Query right = parseUnary();
            left = new Query.And(left, right);
        }
    }

    private Query parseUnary() {
        Token t = peek();
        if (t.type == TokenType.NOT) {
            consume();
            return new Query.Not(parseUnary());
        }
        return parseAtom();
    }

    private Query parseAtom() {
        Token t = peek();
        switch (t.type) {
            case WORD -> {
                consume();
                return new Query.Phrase(List.of(t.text.toLowerCase(Locale.ROOT)));
            }
            case PHRASE -> {
                consume();
                return new Query.Phrase(List.copyOf(t.terms));
            }
            case LPAREN -> {
                consume();
                Query inner = parseOr();
                Token close = peek();
                if (close.type != TokenType.RPAREN) {
                    throw new QueryParseException("expected ')'", close.pos);
                }
                consume();
                return inner;
            }
            case END -> throw new QueryParseException("expected a term or phrase", t.pos);
            default -> throw new QueryParseException("unexpected token", t.pos);
        }
    }

    private Token peek() {
        return tokens.get(index);
    }

    private void consume() {
        if (index < tokens.size() - 1) {
            index++;
        }
    }

    private static List<Token> lex(String s) {
        List<Token> out = new ArrayList<>();
        int n = s.length();
        int i = 0;
        while (i < n) {
            char c = s.charAt(i);
            if (Character.isWhitespace(c)) {
                i++;
            } else if (c == '(') {
                out.add(new Token(TokenType.LPAREN, "(", null, i));
                i++;
            } else if (c == ')') {
                out.add(new Token(TokenType.RPAREN, ")", null, i));
                i++;
            } else if (c == '"') {
                int quotePos = i;
                int j = i + 1;
                StringBuilder inner = new StringBuilder();
                boolean closed = false;
                while (j < n) {
                    char d = s.charAt(j);
                    if (d == '"') {
                        closed = true;
                        break;
                    }
                    inner.append(d);
                    j++;
                }
                if (!closed) {
                    throw new QueryParseException("unterminated quoted phrase", quotePos);
                }
                out.add(new Token(TokenType.PHRASE, null, Tokenizer.tokenize(inner.toString()), quotePos));
                i = j + 1;
            } else {
                int start = i;
                while (i < n) {
                    char d = s.charAt(i);
                    if (Character.isWhitespace(d) || d == '(' || d == ')' || d == '"') {
                        break;
                    }
                    i++;
                }
                String raw = s.substring(start, i);
                switch (raw.toLowerCase(Locale.ROOT)) {
                    case "and" -> out.add(new Token(TokenType.AND, raw, null, start));
                    case "or" -> out.add(new Token(TokenType.OR, raw, null, start));
                    case "not" -> out.add(new Token(TokenType.NOT, raw, null, start));
                    default -> out.add(new Token(TokenType.WORD, raw, null, start));
                }
            }
        }
        if (out.isEmpty()) {
            throw new QueryParseException("empty query", 0);
        }
        out.add(new Token(TokenType.END, "", null, n));
        return out;
    }
}
