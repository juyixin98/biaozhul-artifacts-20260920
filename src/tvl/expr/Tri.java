package tvl.expr;

/** SQL 三值逻辑的三个真值。 */
public enum Tri {
    TRUE,
    FALSE,
    UNKNOWN;

    public static Tri fromBoolean(boolean b) {
        return b ? TRUE : FALSE;
    }
}
