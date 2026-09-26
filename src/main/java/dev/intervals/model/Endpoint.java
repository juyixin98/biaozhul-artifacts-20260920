package dev.intervals.model;

/**
 * 端点（有序线性空间中的一个位置）。
 *
 * <p>用于表示时间点或版本号：
 * <ul>
 *   <li>时间模式：{@code value} 为 epoch 毫秒（{@link Long}）</li>
 *   <li>版本模式：{@code value} 为整数版本号（{@link Long}）</li>
 * </ul>
 *
 * <p>{@code infinity} 为非零整数表示无穷：-1 = 负无穷，+1 = 正无穷；
 * 0 表示有限端点。无穷端点不携带 {@code value}。
 *
 * <p>不可变。
 */
public record Endpoint(long value, int infinity) implements Comparable<Endpoint> {

    public static final int NEG_INF = -1;
    public static final int POS_INF = 1;
    public static final int FINITE = 0;

    public Endpoint {
        if (infinity != NEG_INF && infinity != FINITE && infinity != POS_INF) {
            throw new IllegalArgumentException("infinity 只能为 -1/0/+1，实际: " + infinity);
        }
    }

    public static Endpoint finite(long value) {
        return new Endpoint(value, FINITE);
    }

    public static Endpoint negInf() {
        return new Endpoint(0L, NEG_INF);
    }

    public static Endpoint posInf() {
        return new Endpoint(0L, POS_INF);
    }

    public boolean isFinite() {
        return infinity == FINITE;
    }

    public boolean isNegInf() {
        return infinity == NEG_INF;
    }

    public boolean isPosInf() {
        return infinity == POS_INF;
    }

    public boolean isInfinite() {
        return infinity != FINITE;
    }

    /**
     * 自然序：所有有限值内部按 {@code value} 比较；负无穷小于一切；正无穷大于一切。
     * 注意：同一位置的两个有限端点（如闭端点与开端点）自然序相同，
     * 是否属于集合由 {@link Interval} 的 included 标志区分。
     */
    @Override
    public int compareTo(Endpoint o) {
        if (infinity == FINITE && o.infinity == FINITE) {
            return Long.compare(value, o.value);
        }
        return Integer.compare(infinity, o.infinity);
    }

    @Override
    public String toString() {
        return switch (infinity) {
            case NEG_INF -> "-inf";
            case POS_INF -> "+inf";
            default -> Long.toString(value);
        };
    }
}
