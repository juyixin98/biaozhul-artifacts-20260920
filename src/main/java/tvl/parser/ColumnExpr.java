package tvl.parser;

import tvl.core.Pos;

/** 列引用。列名大小写敏感，解析阶段不知道列是否存在（由类型检查阶段校验）。 */
public final class ColumnExpr extends Expr {
    public final String name;
    private final Pos p;
    private final int len;

    public ColumnExpr(String name, Pos p, int len) {
        this.name = name;
        this.p = p;
        this.len = len;
    }

    @Override public Pos pos() { return p; }
    @Override public int length() { return len; }

    @Override
    public java.util.Map<String, Object> toJson() {
        java.util.Map<String, Object> m = node("Column", p, len);
        m.put("name", name);
        return m;
    }
}
