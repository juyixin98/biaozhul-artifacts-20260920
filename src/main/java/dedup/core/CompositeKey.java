package dedup.core;

/**
 * 事件去重身份：流键 + 事件 ID 的复合键。
 * 同一复合键的不同副本视为同一事件（无论载荷是否一致）。
 */
public record CompositeKey(String key, String id) {

    @Override
    public String toString() {
        return key + "|" + id;
    }
}
