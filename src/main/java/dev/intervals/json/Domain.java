package dev.intervals.json;

/**
 * 计算域：时间点或整数版本号。
 */
public enum Domain {
    /** 端点为 ISO-8601 时间，内部以 epoch 毫秒表示 */
    TIME,
    /** 端点为整数版本号 */
    VERSION;

    public static Domain fromString(String raw) {
        if (raw == null) {
            return TIME;
        }
        return switch (raw.trim().toLowerCase()) {
            case "time", "timestamp", "date" -> TIME;
            case "version", "int", "integer" -> VERSION;
            default -> throw new IllegalArgumentException(
                    "未知 domain: " + raw + "（支持 time / version）");
        };
    }
}
