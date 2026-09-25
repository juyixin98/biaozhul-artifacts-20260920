package booleansearch.query;

import booleansearch.query.QueryLexer.Token;
import booleansearch.query.QueryLexer.Type;

import java.util.ArrayList;
import java.util.List;

/**
 * 递归下降解析器，把查询串解析成 {@link QueryNode} 语法树。
 *
 * <p>所有语法错误都抛 {@link QueryParseException} 并携带字符位置；
 * 不做任何"猜测式修复"（不自动补 AND、不忽略多余 token），位置精确保留。
 */
public final class QueryParser {

    private final List<Token> tokens;
    private int index = 0;

    private QueryParser(String query) {
        this.tokens = QueryLexer.lex(query == null ? "" : query);
    }

    public static QueryNode parse(String query) {
        QueryParser parser = new QueryParser(query);
        if (parser.peek().type() == Type.EOF) {
            throw new QueryParseException("查询为空", 0);
        }
        QueryNode node = parser.parseOr();
        Token leftover = parser.peek();
        if (leftover.type() != Type.EOF) {
            throw new QueryParseException("括号不匹配或存在多余的 '" + leftover.text() + "'",
                    leftover.position());
        }
        return node;
    }

    /** orExpr := andExpr ( OR andExpr )* */
    private QueryNode parseOr() {
        QueryNode first = parseAnd();
        if (peek().type() != Type.OR) {
            return first;
        }
        List<QueryNode> children = new ArrayList<>();
        children.add(first);
        int position = first.position();
        while (peek().type() == Type.OR) {
            next();
            children.add(parseAnd());
        }
        return new QueryNode.Or(children, position);
    }

    /** andExpr := notExpr ( AND notExpr )* */
    private QueryNode parseAnd() {
        QueryNode first = parseNot();
        if (peek().type() != Type.AND) {
            return first;
        }
        List<QueryNode> children = new ArrayList<>();
        children.add(first);
        int position = first.position();
        while (peek().type() == Type.AND) {
            next();
            children.add(parseNot());
        }
        return new QueryNode.And(children, position);
    }

    /** notExpr := NOT notExpr | atom （NOT 可连续，如 NOT NOT a） */
    private QueryNode parseNot() {
        Token token = peek();
        if (token.type() == Type.NOT) {
            next();
            return new QueryNode.Not(parseNot(), token.position());
        }
        return parseAtom();
    }

    /** atom := TERM | '(' expr ')' */
    private QueryNode parseAtom() {
        Token token = peek();
        return switch (token.type()) {
            case TERM -> {
                next();
                yield new QueryNode.Term(token.text(), token.position());
            }
            case LPAREN -> {
                next();
                if (peek().type() == Type.RPAREN) {
                    throw new QueryParseException("括号内缺少表达式", token.position());
                }
                QueryNode inner = parseOr();
                Token closing = peek();
                if (closing.type() != Type.RPAREN) {
                    throw new QueryParseException(
                            "缺少右括号，实际遇到 '" + closing.text() + "'", closing.position());
                }
                next();
                yield inner;
            }
            case AND, OR -> throw new QueryParseException(
                    "运算符 '" + token.text() + "' 缺少左侧操作数", token.position());
            case NOT -> throw new QueryParseException("异常的 NOT", token.position());
            case RPAREN -> throw new QueryParseException("多余的右括号 ')'", token.position());
            case EOF -> throw new QueryParseException("表达式意外结束", token.position());
        };
    }

    private Token peek() {
        return tokens.get(index);
    }

    private Token next() {
        return tokens.get(index++);
    }
}
