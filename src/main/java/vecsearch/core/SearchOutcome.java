package vecsearch.core;

import java.util.List;

/**
 * 一次检索的结果。
 *
 * @param hits         按距离升序的命中（最多 k 条）
 * @param computations 本次检索实际发生的两向量距离计算次数
 * @param exact        true=精确基线（暴力），false=近似索引
 */
public record SearchOutcome(List<SearchHit> hits, long computations, boolean exact) {
}
