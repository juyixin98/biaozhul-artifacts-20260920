package streamagg.core;

import java.math.BigDecimal;
import java.util.Objects;

/**
 * 一条不可变的事件操作。
 *
 * @param eventId 事件 ID（同一事件的全部生命周期共享）
 * @param type    操作类型
 * @param key     归属键（CORRECT 可为空，继承原键；RETRACT 可为空）
 * @param value   数值（ADD 必填；CORRECT 必填，表示更正后的新值；RETRACT 必须为空）
 * @param version 版本号，从 1 开始严格递增；为 {@code null} 表示无版本乱序保护，
 *                由引擎按摄入顺序赋予合成版本
 * @param opId    幂等去重 ID，可为空；非空时同一 opId 的重复提交整体幂等
 */
public record EventOp(String eventId,
                      OpType type,
                      String key,
                      BigDecimal value,
                      Long version,
                      String opId) {

    public EventOp {
        Objects.requireNonNull(eventId, "eventId 不能为空");
        Objects.requireNonNull(type, "type 不能为空");
        if (eventId.isBlank()) {
            throw new IllegalArgumentException("eventId 不能为空串");
        }
        switch (type) {
            case ADD -> {
                if (key == null || key.isBlank()) {
                    throw new IllegalArgumentException("ADD 操作必须带 key");
                }
                if (value == null) {
                    throw new IllegalArgumentException("ADD 操作必须带 value");
                }
            }
            case CORRECT -> {
                if (value == null) {
                    throw new IllegalArgumentException("CORRECT 操作必须带新的 value");
                }
            }
            case RETRACT -> {
                if (value != null) {
                    throw new IllegalArgumentException("RETRACT 操作不允许带 value");
                }
            }
        }
    }

    public static EventOp add(String eventId, String key, BigDecimal value, Long version, String opId) {
        return new EventOp(eventId, OpType.ADD, key, value, version, opId);
    }

    public static EventOp retract(String eventId, Long version, String opId) {
        return new EventOp(eventId, OpType.RETRACT, null, null, version, opId);
    }

    public static EventOp correct(String eventId, String key, BigDecimal value, Long version, String opId) {
        return new EventOp(eventId, OpType.CORRECT, key, value, version, opId);
    }
}
