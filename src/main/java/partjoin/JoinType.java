package partjoin;

/** Supported join types. */
public enum JoinType {
    INNER,
    LEFT;

    public static JoinType parse(String s) {
        if (s == null) throw new JoinException(JoinException.INVALID_REQUEST, "Missing joinType");
        try {
            return valueOf(s.trim().toUpperCase());
        } catch (IllegalArgumentException e) {
            throw new JoinException(JoinException.INVALID_REQUEST, "Unsupported joinType: " + s);
        }
    }
}
