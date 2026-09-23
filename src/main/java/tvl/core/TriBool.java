package tvl.core;

/**
 * 三值逻辑（3VL）的三个真值：TRUE / FALSE / UNKNOWN。
 *
 * Kleene 三值逻辑真值表（SQL 语义）：
 *
 *   NOT:  T->F, F->T, U->U
 *   AND:  U AND T = U, U AND F = F
 *   OR :  U OR  T = T, U OR  F = U
 */
public enum TriBool {
    TRUE,
    FALSE,
    UNKNOWN;

    public TriBool not() {
        switch (this) {
            case TRUE: return FALSE;
            case FALSE: return TRUE;
            default: return UNKNOWN;
        }
    }

    public TriBool and(TriBool other) {
        if (this == FALSE || other == FALSE) {
            return FALSE;
        }
        if (this == TRUE && other == TRUE) {
            return TRUE;
        }
        return UNKNOWN;
    }

    public TriBool or(TriBool other) {
        if (this == TRUE || other == TRUE) {
            return TRUE;
        }
        if (this == FALSE && other == FALSE) {
            return FALSE;
        }
        return UNKNOWN;
    }

    /** 字符串形式与 SQL 保持一致：UNKNOWN 在需要展示为取值时写作 UNKNOWN。 */
    public String toSql() {
        return name();
    }
}
