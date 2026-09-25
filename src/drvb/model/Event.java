package drvb.model;

import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * 一个输入事件。
 *
 * <p>{@code eventTime} 是事件自带的业务时间（决定使用哪个规则版本）；
 * 处理时间（arrival time）由系统在接收时从注入时钟读取，事件本身不携带。
 *
 * <p>{@code payload} 是事件业务字段，规则谓词在此之上求值。
 * 为方便规则编写，求值上下文中另外注入三个保名字段：
 * {@code eventId}、{@code eventTime}、{@code type}；payload 中若存在同名字段，
 * payload 优先（保名字段仅作回退）。
 */
public final class Event {

    private final String eventId;
    private final long eventTime;
    private final String type;
    private final Map<String, Object> payload;

    public Event(String eventId, long eventTime, String type, Map<String, Object> payload) {
        if (eventId == null || eventId.isEmpty()) {
            throw new IllegalArgumentException("eventId 必填且不能为空");
        }
        this.eventId = eventId;
        this.eventTime = eventTime;
        this.type = type;
        this.payload = payload == null
                ? Collections.emptyMap()
                : Collections.unmodifiableMap(new LinkedHashMap<>(payload));
    }

    public String eventId() {
        return eventId;
    }

    public long eventTime() {
        return eventTime;
    }

    public String type() {
        return type;
    }

    public Map<String, Object> payload() {
        return payload;
    }
}
