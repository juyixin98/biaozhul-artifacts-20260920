package phrase.doc;

import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 一篇被检索文档：固定 id + 有序命名字段。
 * 字段用 LinkedHashMap 构造以保留字段顺序（跨字段拼接位置时依赖该顺序）。
 */
public record Doc(String id, Map<String, String> fields) {

    public Doc {
        if (id == null || id.isEmpty()) {
            throw new IllegalArgumentException("doc id must not be empty");
        }
        // 显式保留字段插入顺序（跨字段拼接位置时依赖该顺序）
        fields = Collections.unmodifiableMap(new LinkedHashMap<>(fields));
    }

    public static Doc of(String id, Map<String, String> fields) {
        return new Doc(id, fields);
    }

    public static Doc of(String id, String field, String value) {
        return new Doc(id, Map.of(field, value));
    }

    public String field(String name) {
        return fields.getOrDefault(name, "");
    }

    public List<String> fieldNames() {
        return List.copyOf(fields.keySet());
    }
}
