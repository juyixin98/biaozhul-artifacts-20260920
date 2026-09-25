package streamagg.service;

import java.math.BigDecimal;
import java.util.Map;

import streamagg.core.EventOp;
import streamagg.core.OpType;
import streamagg.json.Json;
import streamagg.json.JsonException;

/** JSON 请求体 <-> 领域对象 的转换。 */
final class Protocol {

    private Protocol() {
    }

    /**
     * 请求体形如：
     * <pre>
     * {"eventId":"e1","op":"ADD","key":"k1","value":10.25,"version":1,"opId":"uuid-1"}
     * </pre>
     */
    static EventOp parseOp(Map<String, Object> body) {
        String eventId = Json.requireString(body, "eventId");
        String opName = Json.requireString(body, "op").toUpperCase(java.util.Locale.ROOT);
        OpType type;
        try {
            type = OpType.valueOf(opName);
        } catch (IllegalArgumentException e) {
            throw new JsonException("未知 op 类型: " + opName + "（允许 ADD/RETRACT/CORRECT）");
        }
        String key = Json.getString(body, "key");
        Object rawValue = body.get("value");
        BigDecimal value = null;
        if (rawValue != null) {
            if (rawValue instanceof BigDecimal bd) {
                value = bd;
            } else if (rawValue instanceof Boolean) {
                throw new JsonException("value 不能是布尔值");
            } else if (rawValue instanceof Number n) {
                value = new BigDecimal(n.toString());
            } else {
                throw new JsonException("value 必须是数字");
            }
        }
        Long version = Json.getLong(body, "version");
        String opId = Json.getString(body, "opId");
        return new EventOp(eventId, type, key, value, version, opId);
    }
}
