package colscan.query;

import java.math.BigInteger;

/**
 * 单个聚合的累加状态。LONG 列的 SUM 使用 BigInteger，避免 long 溢出；
 * AVG 统一返回 double。没有任何非 NULL 输入时，结果为 null（COUNT 除外）。
 */
final class Acc {

    final AggSpec spec;
    final boolean longType; // 输入列是否 LONG（COUNT 按 LONG 处理）

    long count;                 // COUNT(*)：匹配行数；其他：非 NULL 值个数
    private BigInteger longSum; // LONG SUM
    private double doubleSum;   // DOUBLE SUM
    private Number min;
    private Number max;

    Acc(AggSpec spec, boolean longType) {
        this.spec = spec;
        this.longType = longType;
    }

    /** COUNT(*) 对每个匹配行调用一次（包含列为 NULL 的行）。 */
    void addRow() {
        if (spec.func.equals(AggSpec.COUNT)) count++;
    }

    /** 其他聚合对匹配行上的非 NULL 值调用；value 为 null 时忽略。 */
    void addValue(Number value) {
        if (value == null) return;
        switch (spec.func) {
            case AggSpec.COUNT_COL:
                count++;
                break;
            case AggSpec.SUM:
            case AggSpec.AVG:
                count++;
                if (longType) {
                    if (longSum == null) longSum = BigInteger.ZERO;
                    longSum = longSum.add(BigInteger.valueOf(value.longValue()));
                } else {
                    doubleSum += value.doubleValue();
                }
                break;
            case AggSpec.MIN:
                if (min == null || Numbers.compare(value, min) < 0) min = value;
                break;
            case AggSpec.MAX:
                if (max == null || Numbers.compare(value, max) > 0) max = value;
                break;
            default:
                throw new IllegalStateException("bad agg func " + spec.func);
        }
    }

    /** 合并另一个分片上的同构累加器。 */
    void merge(Acc o) {
        if (o == null) return;
        if (spec.func.equals(AggSpec.COUNT) || spec.func.equals(AggSpec.COUNT_COL)) {
            count += o.count;
        }
        if ((spec.func.equals(AggSpec.SUM) || spec.func.equals(AggSpec.AVG))) {
            count += o.count;
            if (longType) {
                if (o.longSum != null) {
                    longSum = (longSum == null ? BigInteger.ZERO : longSum).add(o.longSum);
                }
            } else {
                doubleSum += o.doubleSum;
            }
        }
        if (spec.func.equals(AggSpec.MIN)) {
            if (o.min != null && (min == null || Numbers.compare(o.min, min) < 0)) min = o.min;
        }
        if (spec.func.equals(AggSpec.MAX)) {
            if (o.max != null && (max == null || Numbers.compare(o.max, max) > 0)) max = o.max;
        }
    }

    /** 最终结果：聚合输入为空时返回 null（AVG 无输入也是 null，绝不返回 0）。 */
    Object value() {
        switch (spec.func) {
            case AggSpec.COUNT:
            case AggSpec.COUNT_COL:
                return count;
            case AggSpec.SUM:
                if (count == 0) return null;
                // 不能写三目：long/double 混合条件表达式会把 long 提升为 double
                if (longType) {
                    return longSum.longValueExact();
                }
                return doubleSum;
            case AggSpec.AVG:
                if (count == 0) return null;
                return longType
                        ? new java.math.BigDecimal(longSum)
                                .divide(java.math.BigDecimal.valueOf(count), 10,
                                        java.math.RoundingMode.HALF_UP)
                                .doubleValue()
                        : doubleSum / count;
            case AggSpec.MIN:
                return min;
            case AggSpec.MAX:
                return max;
            default:
                throw new IllegalStateException();
        }
    }
}
