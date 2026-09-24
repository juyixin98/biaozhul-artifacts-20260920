package com.tvl.core;

/**
 * SQL 三值逻辑真值：FALSE / UNKNOWN / TRUE。
 *
 * code 是 NULL 位图向量里使用的紧凑编码：
 *   FALSE=0, UNKNOWN=1, TRUE=2。
 *
 * 真值表（SQL 标准）：
 *   AND: 任一为 FALSE -> FALSE；否则任一为 UNKNOWN -> UNKNOWN；否则 TRUE
 *   OR : 任一为 TRUE  -> TRUE ；否则任一为 UNKNOWN -> UNKNOWN；否则 FALSE
 *   NOT : TRUE<->FALSE，UNKNOWN 保持 UNKNOWN
 *   WHERE 只保留结果为 TRUE 的行（UNKNOWN 与 FALSE 一起被过滤）。
 */
public enum Ternary {
    FALSE((byte) 0),
    UNKNOWN((byte) 1),
    TRUE((byte) 2);

    public final byte code;

    Ternary(byte code) {
        this.code = code;
    }

    public static Ternary ofCode(byte code) {
        switch (code) {
            case 0:
                return FALSE;
            case 1:
                return UNKNOWN;
            case 2:
                return TRUE;
            default:
                throw new IllegalArgumentException("非法三值编码: " + code);
        }
    }

    public static Ternary fromBool(boolean value) {
        return value ? TRUE : FALSE;
    }

    /** WHERE 语义：只有 TRUE 放行。 */
    public boolean isSelected() {
        return this == TRUE;
    }

    public Ternary and(Ternary other) {
        return ofCode(andCode(this.code, other.code));
    }

    public Ternary or(Ternary other) {
        return ofCode(orCode(this.code, other.code));
    }

    public Ternary not() {
        return ofCode(notCode(this.code));
    }

    /* ---------- 向量（逐字节）版本，供位图执行器调用，避免装箱 ---------- */

    public static byte andCode(byte a, byte b) {
        if (a == FALSE.code || b == FALSE.code) {
            return FALSE.code;
        }
        if (a == UNKNOWN.code || b == UNKNOWN.code) {
            return UNKNOWN.code;
        }
        return TRUE.code;
    }

    public static byte orCode(byte a, byte b) {
        if (a == TRUE.code || b == TRUE.code) {
            return TRUE.code;
        }
        if (a == UNKNOWN.code || b == UNKNOWN.code) {
            return UNKNOWN.code;
        }
        return FALSE.code;
    }

    public static byte notCode(byte a) {
        switch (a) {
            case 0:
                return TRUE.code;
            case 2:
                return FALSE.code;
            default:
                return UNKNOWN.code;
        }
    }
}
