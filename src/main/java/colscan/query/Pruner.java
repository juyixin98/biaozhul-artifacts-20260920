package colscan.query;

import colscan.store.ColumnStats;

/**
 * 列式分区裁剪器。
 *
 * 原则：统计信息只能用于排除“确定不命中”的分片；
 * 任何不确定（统计缺失、全 NULL、谓词语义上可能命中 NULL/其他值）的情况都必须扫描。
 */
public final class Pruner {

    // 跳过原因
    public static final String SCAN_STATS_MISSING = "scan:stats_missing";
    public static final String SCAN_RANGE_OVERLAPS = "scan:range_overlaps";
    public static final String SCAN_NE_UNKNOWN = "scan:ne_other_values_possible";
    public static final String PRUNED_ALL_NULL = "pruned:all_null_column";
    public static final String PRUNED_NO_OVERLAP = "pruned:no_range_overlap";
    public static final String PRUNED_NE_CONSTANT = "pruned:ne_constant_equal";

    public static final class Decision {
        public final boolean prune;
        public final String reason;

        Decision(boolean prune, String reason) {
            this.prune = prune;
            this.reason = reason;
        }
    }

    private Pruner() {}

    /**
     * @param stats 该分片过滤列的统计；为 null 表示该分片统计缺失
     */
    public static Decision decide(ColumnStats stats, Filter f) {
        // 统计缺失：必须扫描（无法证明任何事）
        if (stats == null) {
            return new Decision(false, SCAN_STATS_MISSING);
        }
        // 全 NULL 列（min/max 均缺失）：任何比较谓词对 NULL 都不命中，确定不命中，可裁剪
        if (stats.allNull()) {
            return new Decision(true, PRUNED_ALL_NULL);
        }

        Number min = stats.min;
        Number max = stats.max;
        switch (f.op) {
            case Filter.EQ:
                // 仅当值严格落在 (min,max) 之外才可排除；边界相等必须扫描
                if (Numbers.compare(f.value, min) < 0 || Numbers.compare(f.value, max) > 0) {
                    return new Decision(true, PRUNED_NO_OVERLAP);
                }
                return new Decision(false, SCAN_RANGE_OVERLAPS);

            case Filter.NE:
                // x != v 对所有行（含 NULL，NULL 本来就不命中）都为假
                // ⟺ 分片是常量分片且常量 == v，此时才能裁剪。
                // 常量分片但常量 != v：所有非 NULL 行都命中，必须扫描；
                // 范围分片：可能存在 != v 的值，必须扫描。
                boolean constant = Numbers.compare(min, max) == 0;
                if (constant && Numbers.compare(min, f.value) == 0) {
                    return new Decision(true, PRUNED_NE_CONSTANT);
                }
                return new Decision(false, SCAN_NE_UNKNOWN);

            case Filter.LT:
                // x < v 对所有值都为假 ⟺ min >= v。
                // min == v 是边界相等：最小值行不满足 x<v，且无更小值，可裁剪；
                // max == v 时可能存在更小的值，必须扫描。
                if (Numbers.compare(min, f.value) >= 0) {
                    return new Decision(true, PRUNED_NO_OVERLAP);
                }
                return new Decision(false, SCAN_RANGE_OVERLAPS);

            case Filter.LE: // x <= v：max <= v 必扫描（边界相等要命中）；min > v 才可排除
                if (Numbers.compare(min, f.value) > 0) {
                    return new Decision(true, PRUNED_NO_OVERLAP);
                }
                return new Decision(false, SCAN_RANGE_OVERLAPS);

            case Filter.GT:
                // x > v 可排除的条件：max <= v。max == v 是边界相等：x>v 不命中，可排除；
                // min == v 时分片内可能有更大的值，必须扫描。
                if (Numbers.compare(max, f.value) <= 0) {
                    return new Decision(true, PRUNED_NO_OVERLAP);
                }
                return new Decision(false, SCAN_RANGE_OVERLAPS);

            case Filter.GE: // x >= v：max < v 才可排除；max == v 边界相等必须扫描
                if (Numbers.compare(max, f.value) < 0) {
                    return new Decision(true, PRUNED_NO_OVERLAP);
                }
                return new Decision(false, SCAN_RANGE_OVERLAPS);

            default:
                return new Decision(false, SCAN_RANGE_OVERLAPS);
        }
    }
}
