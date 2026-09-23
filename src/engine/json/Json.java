package engine.json;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;

/**
 * 极简 JSON 值模型（只支持本引擎需要的七种值）：
 * null、布尔、64 位整数、双精度浮点、字符串、数组、对象。
 *
 * <p>数值优先存为 {@link JLong}：仅当词法为整数且能放入 long 时成立，
 * 否则退化为 {@link JDouble}。本引擎的窗口输入/输出只允许整数列。
 */
public interface Json {

    /** JSON null 单例。 */
    final class JNull implements Json {
        public static final JNull V = new JNull();
        private JNull() {
        }
    }

    record JBool(boolean value) implements Json {
    }

    record JLong(long value) implements Json {
    }

    record JDouble(double value) implements Json {
    }

    record JStr(String value) implements Json {
    }

    final class JArr implements Json {
        public final List<Json> items = new ArrayList<>();

        public void add(Json v) {
            items.add(v);
        }
    }

    final class JObj implements Json {
        public final LinkedHashMap<String, Json> members = new LinkedHashMap<>();

        public void put(String key, Json value) {
            members.put(key, value);
        }

        public Json get(String key) {
            return members.get(key);
        }

        public Json require(String key) {
            Json v = members.get(key);
            if (v == null) {
                throw new JsonException("JSON 对象缺少必需字段 '" + key + "'");
            }
            return v;
        }
    }
}
