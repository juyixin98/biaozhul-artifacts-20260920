package tvl.parser;

import tvl.core.Pos;

/** 逻辑非：NOT x（NOT 是一元前缀操作符，优先级高于 AND）。 */
public final class NotExpr extends Expr {
    public final Expr operand;
    private final Pos p;
    private final int len;

    public NotExpr(Expr operand, Pos notPos, int endOffset) {
        this.operand = operand;
        this.p = notPos;
        this.len = endOffset - notPos.offset;
    }

    @Override public Pos pos() { return p; }
    @Override public int length() { return len; }

    @Override
    public java.util.Map<String, Object> toJson() {
        java.util.Map<String, Object> m = node("Not", p, len);
        m.put("operand", operand.toJson());
        return m;
    }
}
