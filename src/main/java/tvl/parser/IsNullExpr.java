package tvl.parser;

import tvl.core.Pos;

/** x IS NULL / x IS NOT NULL 谓词。 */
public final class IsNullExpr extends Expr {
    public final Expr operand;
    public final boolean negated; // true 表示 IS NOT NULL
    private final Pos p;
    private final int len;

    public IsNullExpr(Expr operand, boolean negated, Pos start, int endOffset) {
        this.operand = operand;
        this.negated = negated;
        this.p = start;
        this.len = endOffset - start.offset;
    }

    @Override public Pos pos() { return p; }
    @Override public int length() { return len; }

    @Override
    public java.util.Map<String, Object> toJson() {
        java.util.Map<String, Object> m = node(negated ? "IsNotNull" : "IsNull", p, len);
        m.put("operand", operand.toJson());
        return m;
    }
}
