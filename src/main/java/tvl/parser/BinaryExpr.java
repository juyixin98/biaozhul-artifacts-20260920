package tvl.parser;

import tvl.core.Pos;

/**
 * 二元表达式，统一覆盖：
 *  - 算术：+ - * /
 *  - 比较：= &lt;&gt; &lt; &lt;= &gt; &gt;=
 *  - 逻辑：AND OR
 *
 * 具体合法性与结果类型由 TypeChecker 决定；求值时按 op 分类处理。
 * opPos 指向操作符自身，便于报“操作数类型不匹配”时定位到操作符。
 */
public final class BinaryExpr extends Expr {
    public final TokenType op;
    public final String opText;
    public final Expr left;
    public final Expr right;
    public final Pos opPos;
    public final int opLength;
    private final Pos p;
    private final int len;

    public BinaryExpr(TokenType op, String opText, Expr left, Expr right,
                      Pos start, Pos opPos, int opLength, int endOffset) {
        this.op = op;
        this.opText = opText;
        this.left = left;
        this.right = right;
        this.opPos = opPos;
        this.opLength = opLength;
        this.p = start;
        this.len = endOffset - start.offset;
    }

    @Override public Pos pos() { return p; }
    @Override public int length() { return len; }

    public boolean isArithmetic() {
        return op == TokenType.PLUS || op == TokenType.MINUS
                || op == TokenType.STAR || op == TokenType.SLASH;
    }

    public boolean isComparison() {
        return op == TokenType.EQ || op == TokenType.NE
                || op == TokenType.LT || op == TokenType.LE
                || op == TokenType.GT || op == TokenType.GE;
    }

    public boolean isLogic() {
        return op == TokenType.AND || op == TokenType.OR;
    }

    @Override
    public java.util.Map<String, Object> toJson() {
        java.util.Map<String, Object> m = node("Binary", p, len);
        m.put("op", opText);
        m.put("opPos", opPos.toJson());
        m.put("left", left.toJson());
        m.put("right", right.toJson());
        return m;
    }
}
