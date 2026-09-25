package com.example.ac;

/**
 * 空模式（""）处理策略。
 *
 * <ul>
 *   <li>{@link #ERROR}：编译时只要存在空模式就拒绝；</li>
 *   <li>{@link #SKIP}：忽略空模式，被忽略的 id 记入编译结果；</li>
 *   <li>{@link #MATCH_EVERY_POSITION}：空模式在每个 code point 边界产生一条
 *       零长度命中（长度 n 的文本共有 n+1 个位置：0..n），流式分片与整体匹配语义一致。</li>
 * </ul>
 */
public enum EmptyPatternPolicy {
    ERROR,
    SKIP,
    MATCH_EVERY_POSITION;

    public static EmptyPatternPolicy fromString(String raw) {
        if (raw == null) {
            return MATCH_EVERY_POSITION;
        }
        return switch (raw.trim().toUpperCase()) {
            case "ERROR" -> ERROR;
            case "SKIP" -> SKIP;
            case "MATCH_EVERY_POSITION", "MATCH_EVERY", "EVERY_POSITION" -> MATCH_EVERY_POSITION;
            default -> throw new IllegalArgumentException(
                    "unknown emptyPatternPolicy: " + raw
                    + " (expected ERROR, SKIP or MATCH_EVERY_POSITION)");
        };
    }
}
