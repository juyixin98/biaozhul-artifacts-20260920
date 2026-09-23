package colscan.query;

/**
 * 范围 / 比较过滤谓词。NULL 语义与 SQL 一致：
 * 值为 NULL 的行，谓词结果为 UNKNOWN（不命中）。
 */
public final class Filter {

    public static final String EQ = "eq";
    public static final String NE = "ne";
    public static final String LT = "lt";
    public static final String LE = "le";
    public static final String GT = "gt";
    public static final String GE = "ge";

    public final String column;
    public final String op;
    public final Number value;

    public Filter(String column, String op, Number value) {
        this.column = column;
        this.op = op;
        this.value = value;
    }

    public static boolean isValidOp(String op) {
        return EQ.equals(op) || NE.equals(op) || LT.equals(op) || LE.equals(op)
                || GT.equals(op) || GE.equals(op);
    }

    /** 行级求值：v 为 null 一律 false（NULL 绝不参与比较，更不会当成 0）。 */
    public boolean matches(Object v) {
        if (v == null) return false;
        int c = Numbers.compare((Number) v, value);
        switch (op) {
            case EQ: return c == 0;
            case NE: return c != 0;
            case LT: return c < 0;
            case LE: return c <= 0;
            case GT: return c > 0;
            case GE: return c >= 0;
            default: throw new IllegalStateException("unknown op " + op);
        }
    }
}
