package tvl.parser;

import tvl.core.Pos;

/** 一元负号：-x。 */
public final class UnaryMinusExpr extends Expr {
    public final Expr operand;
    private final Pos p;
    private final int len;

    public UnaryMinusExpr(Expr operand, Pos p, int endOffset) {
        this.operand = operand;
        this.p = p;
        this.len = endOffset - p.offset;
    }

    @Override public Pos pos() { return p; }
    @Override public int length() { return len; }

    @Override
    public java.util.Map<String, Object> toJson() {
        java.util.Map<String, Object> m = node("UnaryMinus", p, len);
        m.put("operand", operand.toJson());
        return m;
    }
}
