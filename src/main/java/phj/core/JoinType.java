package phj.core;

/** 连接类型。 */
public enum JoinType {
    INNER,
    LEFT;

    public static JoinType parse(String s) {
        if (s == null) return INNER;
        return switch (s.trim().toUpperCase()) {
            case "INNER", "INNER JOIN", "JOIN" -> INNER;
            case "LEFT", "LEFT JOIN", "LEFT OUTER JOIN" -> LEFT;
            default -> throw new IllegalArgumentException("不支持的 joinType：'" + s + "'（仅支持 INNER / LEFT）");
        };
    }
}
