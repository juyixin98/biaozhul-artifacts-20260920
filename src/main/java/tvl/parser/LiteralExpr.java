package tvl.parser;

import tvl.core.Pos;

/** 整数、字符串、布尔、NULL 字面量。 */
public final class LiteralExpr extends Expr {
    public enum Kind { INTEGER, STRING, BOOLEAN, NULL }

    public final Kind kind;
    public final String rawText;
    public final Object value; // Long / String / Boolean / null
    public final int len;
    private final Pos p;

    public LiteralExpr(Kind kind, String rawText, Object value, Pos p, int len) {
        this.kind = kind;
        this.rawText = rawText;
        this.value = value;
        this.p = p;
        this.len = len;
    }

    @Override public Pos pos() { return p; }
    @Override public int length() { return len; }

    @Override
    public java.util.Map<String, Object> toJson() {
        java.util.Map<String, Object> m = node("Literal", p, len);
        m.put("literalKind", kind.name());
        m.put("raw", rawText);
        m.put("value", value);
        return m;
    }
}
