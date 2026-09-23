package com.example.tvl.engine;

/**
 * SQL 三值逻辑。向量执行中用字节编码：{@link #code()}。
 *
 * <pre>
 * NOT:  TRUE→FALSE, FALSE→TRUE, UNKNOWN→UNKNOWN
 * AND:  任一 FALSE 即 FALSE；否则任一 UNKNOWN 即 UNKNOWN；否则 TRUE
 * OR:   任一 TRUE 即 TRUE；否则任一 UNKNOWN 即 UNKNOWN；否则 FALSE
 * </pre>
 */
public enum SqlBool {
    TRUE(1),
    FALSE(0),
    UNKNOWN(2);

    private final int code;

    SqlBool(int code) {
        this.code = code;
    }

    public int code() {
        return code;
    }

    public static SqlBool fromCode(int code) {
        return switch (code) {
            case 1 -> TRUE;
            case 0 -> FALSE;
            case 2 -> UNKNOWN;
            default -> throw new IllegalArgumentException("非法三值编码: " + code);
        };
    }

    public SqlBool not() {
        return switch (this) {
            case TRUE -> FALSE;
            case FALSE -> TRUE;
            case UNKNOWN -> UNKNOWN;
        };
    }

    public static SqlBool and(SqlBool a, SqlBool b) {
        if (a == FALSE || b == FALSE) {
            return FALSE;
        }
        if (a == UNKNOWN || b == UNKNOWN) {
            return UNKNOWN;
        }
        return TRUE;
    }

    public static SqlBool or(SqlBool a, SqlBool b) {
        if (a == TRUE || b == TRUE) {
            return TRUE;
        }
        if (a == UNKNOWN || b == UNKNOWN) {
            return UNKNOWN;
        }
        return FALSE;
    }
}
