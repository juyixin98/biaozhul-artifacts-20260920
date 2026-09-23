package com.tjoin.core;

/**
 * 区间连接配置。
 *
 * <p>连接条件（{@code L} 为左事件时间，{@code R} 为右事件时间）：
 * <pre>
 *     L + lowerBound &lt;= R &lt;= L + upperBound
 * </pre>
 * 两个边界的闭/开均可单独配置（默认两端闭区间，与 Flink interval join 语义一致）。
 *
 * <p>{@code maxBufferedPerSide} 限制每侧缓冲的事件条数（防止水位线停滞时缓冲无限增长），
 * &lt;=0 表示不限制。超出限制时抛出 {@link BufferCapacityExceededException}。
 */
public final class JoinConfig {

    private final long lowerBound;
    private final long upperBound;
    private final boolean lowerInclusive;
    private final boolean upperInclusive;
    private final int maxBufferedPerSide;

    private JoinConfig(Builder b) {
        if (b.lowerBound > b.upperBound) {
            throw new IllegalArgumentException(
                    "lowerBound (" + b.lowerBound + ") must be <= upperBound (" + b.upperBound + ")");
        }
        this.lowerBound = b.lowerBound;
        this.upperBound = b.upperBound;
        this.lowerInclusive = b.lowerInclusive;
        this.upperInclusive = b.upperInclusive;
        this.maxBufferedPerSide = b.maxBufferedPerSide;
    }

    public static Builder builder() {
        return new Builder();
    }

    /** 对称闭区间 {@code [-bound, +bound]} 的便捷构造。 */
    public static JoinConfig symmetric(long bound) {
        return builder().lowerBound(-bound).upperBound(bound).build();
    }

    public long lowerBound() {
        return lowerBound;
    }

    public long upperBound() {
        return upperBound;
    }

    public boolean lowerInclusive() {
        return lowerInclusive;
    }

    public boolean upperInclusive() {
        return upperInclusive;
    }

    public int maxBufferedPerSide() {
        return maxBufferedPerSide;
    }

    /**
     * 判断时间戳为 {@code leftTs} 的左事件与时间戳为 {@code rightTs} 的右事件是否匹配。
     * 减法比较，避免加法溢出。
     */
    public boolean matches(long leftTs, long rightTs) {
        long d = rightTs - leftTs; // 本身可能溢出，但与有限的上下界（通常远小于 Long 极值）比较仍然安全
        boolean lowerOk = lowerInclusive ? d >= lowerBound : d > lowerBound;
        boolean upperOk = upperInclusive ? d <= upperBound : d < upperBound;
        return lowerOk && upperOk;
    }

    public static final class Builder {
        private long lowerBound = 0L;
        private long upperBound = 0L;
        private boolean lowerInclusive = true;
        private boolean upperInclusive = true;
        private int maxBufferedPerSide = 0;

        public Builder lowerBound(long lowerBound) {
            this.lowerBound = lowerBound;
            return this;
        }

        public Builder upperBound(long upperBound) {
            this.upperBound = upperBound;
            return this;
        }

        public Builder lowerInclusive(boolean inclusive) {
            this.lowerInclusive = inclusive;
            return this;
        }

        public Builder upperInclusive(boolean inclusive) {
            this.upperInclusive = inclusive;
            return this;
        }

        public Builder maxBufferedPerSide(int max) {
            this.maxBufferedPerSide = max;
            return this;
        }

        public JoinConfig build() {
            return new JoinConfig(this);
        }
    }
}
