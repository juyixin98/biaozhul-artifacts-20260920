package dev.dedup.hll;

import java.util.Objects;

/**
 * HLL 草图的不可变配置。配置是合并兼容性的组成部分：
 * 精度或哈希标识不同的草图禁止合并（见 {@link #isCompatibleWith}）。
 *
 * @param precision 精度 p，寄存器数 m = 2^p，允许范围 [4, 18]
 * @param hashId    固定哈希算法标识（见 {@link Murmur3Hash128#HASH_ID}）
 */
public final class HllConfig {

    public static final int MIN_PRECISION = 4;
    public static final int MAX_PRECISION = 18;

    private final int precision;
    private final String hashId;

    public HllConfig(int precision) {
        this(precision, Murmur3Hash128.HASH_ID);
    }

    public HllConfig(int precision, String hashId) {
        if (precision < MIN_PRECISION || precision > MAX_PRECISION) {
            throw new IllegalArgumentException(
                    "precision p 必须在 [" + MIN_PRECISION + "," + MAX_PRECISION + "] 内，收到: " + precision);
        }
        if (hashId == null || hashId.isEmpty()) {
            throw new IllegalArgumentException("hashId 不能为空");
        }
        this.precision = precision;
        this.hashId = hashId;
    }

    public int precision() {
        return precision;
    }

    public int registerCount() {
        return 1 << precision;
    }

    public String hashId() {
        return hashId;
    }

    /**
     * 合并兼容性检查：精度（寄存器数）与哈希算法标识必须完全一致。
     * 不一致即拒绝，不做任何静默降精度/重哈希处理。
     */
    public boolean isCompatibleWith(HllConfig other) {
        return this.precision == other.precision
                && this.hashId.equals(other.hashId);
    }

    /** 返回不兼容原因；兼容时返回 null。 */
    public String incompatibilityReason(HllConfig other) {
        if (other == null) {
            return "对方配置为 null";
        }
        if (this.precision != other.precision) {
            return "精度不一致: p=" + this.precision + " vs p=" + other.precision
                    + "（寄存器数 " + registerCount() + " vs " + other.registerCount() + "），禁止合并";
        }
        if (!this.hashId.equals(other.hashId)) {
            return "哈希算法标识不一致: " + this.hashId + " vs " + other.hashId + "，禁止合并";
        }
        return null;
    }

    @Override
    public boolean equals(Object o) {
        if (this == o) {
            return true;
        }
        if (!(o instanceof HllConfig)) {
            return false;
        }
        HllConfig other = (HllConfig) o;
        return precision == other.precision && hashId.equals(other.hashId);
    }

    @Override
    public int hashCode() {
        return Objects.hash(precision, hashId);
    }

    @Override
    public String toString() {
        return "HllConfig{p=" + precision + ", m=" + registerCount() + ", hash=" + hashId + "}";
    }
}
