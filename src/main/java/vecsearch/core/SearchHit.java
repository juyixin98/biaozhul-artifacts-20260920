package vecsearch.core;

import java.util.Map;

/** 单条近邻命中：向量 ID、到查询向量的真实距离、命中向量的标签。 */
public record SearchHit(String id, float distance, Map<String, String> filter) {
}
