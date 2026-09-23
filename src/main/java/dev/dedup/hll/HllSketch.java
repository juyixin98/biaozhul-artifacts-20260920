package dev.dedup.hll;

import java.nio.charset.StandardCharsets;
import java.util.Arrays;

/**
 * HyperLogLog 草图（单机内存）。
 *
 * 寄存器语义（经典 HLL）：
 *  - 64 位哈希 h1 的高 p 位作寄存器索引 idx；
 *  - 剩余 q=(64-p) 位左移到高位，rank = clz(.)+1，范围 1..q+1（全零时为 q+1，
 *    通过在第 p-1 位放置哨兵 1 实现，无需特判 0），寄存器存最大 rank；
 *  - 0 表示该寄存器从未被命中（线性计数依赖此哨兵）。
 *
 * 基数估计（注意：本类任何输出都是<b>估计值</b>，不是精确计数）：
 *  - 小基数使用线性计数 E = m * ln(m / V)（V 为零寄存器数）；
 *  - 否则使用调和平均 alpha_m * m^2 / sum(2^-R_j)；
 *  - 经典相对标准误差 RSE ≈ 1.04/sqrt(m)。
 *
 * 未实现 HLL++ 的经验偏差修正（empirical bias correction）；已在 README 中明确说明，
 * 因此小基数区间统一依赖线性计数。p=4 时 rank 上界 61，可存入 byte。
 */
public final class HllSketch {

    private final HllConfig config;
    private final byte[] registers;

    public HllSketch(HllConfig config) {
        this.config = config;
        this.registers = new byte[config.registerCount()];
    }

    /** 反序列化/复制用构造器：接管传入数组（调用方不得再修改）。 */
    HllSketch(HllConfig config, byte[] registers) {
        if (registers.length != config.registerCount()) {
            throw new IllegalArgumentException("寄存器数组长度与精度不符");
        }
        this.config = config;
        this.registers = registers;
    }

    public HllConfig config() {
        return config;
    }

    /** 寄存器数 m。 */
    public int registerCount() {
        return registers.length;
    }

    /** 只读寄存器视图（测试/导出使用）。 */
    public byte[] registersSnapshot() {
        return registers.clone();
    }

    byte[] rawRegisters() {
        return registers;
    }

    /** 插入一个已经规范化编码的值。 */
    public void add(byte[] canonicalBytes) {
        addHash(Murmur3Hash128.hash128(canonicalBytes)[0]);
    }

    /** 插入字符串（按 UTF-8 编码后哈希）。 */
    public void addString(String s) {
        add(s.getBytes(StandardCharsets.UTF_8));
    }

    /** 插入一个 64 位哈希值（确定性测试/统计脚本直接注入用）。 */
    public void addHash(long h1) {
        int p = config.precision();
        int idx = (int) (h1 >>> (64 - p));
        long w = (h1 << p) | (1L << (p - 1));
        byte rank = (byte) (Long.numberOfLeadingZeros(w) + 1);
        if (rank > registers[idx]) {
            registers[idx] = rank;
        }
    }

    /** 非零寄存器数。 */
    public int nonzeroRegisters() {
        int nz = 0;
        for (byte r : registers) {
            if (r != 0) {
                nz++;
            }
        }
        return nz;
    }

    /** 零寄存器数 V。 */
    public int zeroRegisters() {
        return registers.length - nonzeroRegisters();
    }

    private double alpha() {
        int m = registers.length;
        switch (m) {
            case 16:
                return 0.673;
            case 32:
                return 0.697;
            case 64:
                return 0.709;
            default:
                return 0.7213 / (1.0 + 1.079 / m);
        }
    }

    /**
     * 原始基数估计（double，未取整）。仅供内部/统计使用；对外请用 {@link #estimate()}。
     */
    public double estimateRaw() {
        int m = registers.length;
        double sum = 0.0;
        int zeros = 0;
        for (byte r : registers) {
            sum += 1.0 / (1L << r); // r==0 时该项为 1
            if (r == 0) {
                zeros++;
            }
        }
        double raw = alpha() * (double) m * (double) m / sum;
        // 小基数线性计数（经典阈值）
        if (raw <= 2.5 * m && zeros > 0) {
            return m * Math.log((double) m / zeros);
        }
        return raw;
    }

    /**
     * 估计基数（非负，四舍五入为 long）。
     * <b>这是近似值，不是精确计数。</b>
     */
    public long estimate() {
        double e = estimateRaw();
        if (e < 0 || Double.isNaN(e) || Double.isInfinite(e)) {
            return 0L;
        }
        return Math.round(e);
    }

    /** 经典理论相对标准误差 1.04/sqrt(m)，例如 p=12 时约 0.026。 */
    public double relativeStandardError() {
        return 1.04 / Math.sqrt(registers.length);
    }

    /**
     * 把另一个同配置草图合并进本草图（逐寄存器取最大 rank）。
     *
     * @throws IncompatibleSketchException 精度或哈希标识不一致
     */
    public void mergeWith(HllSketch other) {
        String reason = config.incompatibilityReason(other.config);
        if (reason != null) {
            throw new IncompatibleSketchException(reason);
        }
        byte[] a = this.registers;
        byte[] b = other.registers;
        for (int i = 0; i < a.length; i++) {
            if (b[i] > a[i]) {
                a[i] = b[i];
            }
        }
    }

    /** 不修改入参的静态合并，返回新草图。 */
    public static HllSketch merge(HllSketch a, HllSketch b) {
        HllSketch out = new HllSketch(a.config);
        out.mergeWith(a);
        out.mergeWith(b);
        return out;
    }

    /** 深拷贝。 */
    public HllSketch copy() {
        return new HllSketch(config, registers.clone());
    }

    @Override
    public boolean equals(Object o) {
        if (this == o) {
            return true;
        }
        if (!(o instanceof HllSketch)) {
            return false;
        }
        HllSketch other = (HllSketch) o;
        return config.equals(other.config) && Arrays.equals(registers, other.registers);
    }

    @Override
    public int hashCode() {
        return 31 * config.hashCode() + Arrays.hashCode(registers);
    }
}
