package streamagg.core;

import java.math.BigDecimal;
import java.util.Objects;

/**
 * 单个键的聚合结果。{@code count} 是该键当前存活事件数；{@code sum} 是存活事件值之和。
 * 使用 BigDecimal 精确表示，不做浮点运算。
 */
public final class KeyStats {
    public static final KeyStats ZERO = new KeyStats(0L, BigDecimal.ZERO);

    private final long count;
    private final BigDecimal sum;

    public KeyStats(long count, BigDecimal sum) {
        if (count < 0) {
            throw new IllegalStateException("禁止计数负漂移: count=" + count);
        }
        this.count = count;
        this.sum = Objects.requireNonNull(sum);
    }

    public long count() {
        return count;
    }

    public BigDecimal sum() {
        return sum;
    }

    public KeyStats add(BigDecimal v) {
        return new KeyStats(count + 1, sum.add(v));
    }

    public KeyStats remove(BigDecimal v) {
        return new KeyStats(count - 1, sum.subtract(v));
    }

    public KeyStats replace(BigDecimal oldValue, BigDecimal newValue) {
        return new KeyStats(count, sum.subtract(oldValue).add(newValue));
    }

    @Override
    public boolean equals(Object o) {
        if (this == o) return true;
        if (!(o instanceof KeyStats other)) return false;
        return count == other.count && sum.compareTo(other.sum) == 0;
    }

    @Override
    public int hashCode() {
        return Objects.hash(count, sum.stripTrailingZeros());
    }

    @Override
    public String toString() {
        return "KeyStats{count=" + count + ", sum=" + sum.toPlainString() + "}";
    }
}
