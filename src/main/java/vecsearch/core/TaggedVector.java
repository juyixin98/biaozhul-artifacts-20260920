package vecsearch.core;

import java.util.Arrays;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * 一条带标签的向量。
 *
 * <p>{@code id} 为字符串（兼容 UUID / 业务 ID）；{@code filter} 为不可变的标签键值对，
 * 检索时按 "全部相等"（AND）匹配；为 null 或空表示无标签。
 */
public final class TaggedVector {
    private final String id;
    private final float[] vector;
    private final Map<String, String> filter;

    public TaggedVector(String id, float[] vector, Map<String, String> filter) {
        if (id == null || id.isEmpty()) {
            throw new IllegalArgumentException("id must not be null or empty");
        }
        if (vector == null || vector.length == 0) {
            throw new IllegalArgumentException("vector must not be null or empty");
        }
        this.id = id;
        this.vector = vector;
        if (filter == null || filter.isEmpty()) {
            this.filter = Collections.emptyMap();
        } else {
            Map<String, String> copy = new LinkedHashMap<>(filter);
            this.filter = Collections.unmodifiableMap(copy);
        }
    }

    public String id() {
        return id;
    }

    public float[] vector() {
        return vector;
    }

    public Map<String, String> filter() {
        return filter;
    }

    /** 过滤条件：标签中必须包含请求的全部键值对。空条件匹配所有向量。 */
    public boolean matches(Map<String, String> requested) {
        if (requested == null || requested.isEmpty()) {
            return true;
        }
        for (Map.Entry<String, String> e : requested.entrySet()) {
            String got = filter.get(e.getKey());
            if (got == null || !got.equals(e.getValue())) {
                return false;
            }
        }
        return true;
    }

    @Override
    public String toString() {
        return "TaggedVector{id=" + id + ", dim=" + vector.length
                + ", filter=" + filter + ", vector=" + Arrays.toString(vector) + '}';
    }
}
