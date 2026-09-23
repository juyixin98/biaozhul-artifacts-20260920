package vecq;

/**
 * SQL 风格三值逻辑（3-valued logic）：TRUE / FALSE / UNKNOWN。
 *
 * 谓词遇到 NULL 操作数时结果为 UNKNOWN；只有 TRUE 才会让行进入选择向量。
 * NOT UNKNOWN = UNKNOWN，AND/OR 遵循 Kleene 真值表。
 *
 * 用 byte 编码（0=FALSE, 1=TRUE, 2=UNKNOWN），便于在 byte[] 批次上做无分支向量化循环。
 */
public final class Tri {

    public static final byte FALSE = 0;
    public static final byte TRUE = 1;
    public static final byte UNKNOWN = 2;

    private Tri() {}

    public static byte not(byte a) {
        return switch (a) {
            case TRUE -> FALSE;
            case FALSE -> TRUE;
            default -> UNKNOWN;
        };
    }

    public static byte and(byte a, byte b) {
        if (a == FALSE || b == FALSE) return FALSE;
        if (a == TRUE && b == TRUE) return TRUE;
        return UNKNOWN;
    }

    public static byte or(byte a, byte b) {
        if (a == TRUE || b == TRUE) return TRUE;
        if (a == FALSE && b == FALSE) return FALSE;
        return UNKNOWN;
    }

    public static String name(byte v) {
        return switch (v) {
            case TRUE -> "TRUE";
            case FALSE -> "FALSE";
            default -> "UNKNOWN";
        };
    }
}
