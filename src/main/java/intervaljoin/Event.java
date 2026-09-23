package intervaljoin;

import java.util.Objects;

/**
 * 一条流事件：来自左流或右流，按 key 分组，带事件时间 ts。
 * id 仅用于结果标识/断言，不参与连接逻辑。
 */
public final class Event {
    public final String side;   // "left" 或 "right"
    public final String key;
    public final long ts;
    public final String id;

    public Event(String side, String key, long ts, String id) {
        if (!"left".equals(side) && !"right".equals(side)) {
            throw new IllegalArgumentException("side must be 'left' or 'right', got: " + side);
        }
        this.side = side;
        this.key = Objects.requireNonNull(key, "key");
        this.ts = ts;
        this.id = id == null ? side + ":" + key + ":" + ts : id;
    }

    @Override
    public String toString() {
        return side + "(" + id + ", key=" + key + ", ts=" + ts + ")";
    }
}
