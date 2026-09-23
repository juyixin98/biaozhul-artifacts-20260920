package dev.dedup.hll;

import java.nio.charset.StandardCharsets;

/**
 * 固定哈希算法：MurmurHash3 x64 128-bit（Austin Appleby 公有领域算法）。
 *
 * 设计约束（不可在版本内更改，否则不同进程/分片产生的草图无法合并）：
 *  - 算法固定为 MurmurHash3 x64_128；
 *  - 种子固定为 {@link #FIXED_SEED}；
 *  - HLL 只使用 128 位输出中的低 64 位 h1（寄存器索引与前导零判定均取自 h1）。
 *
 * 正确性由 results/hash_kat_reference.py（独立 Python 移植版）生成的
 * results/hash-kat-vectors.json 交叉验证，见 HashAlgorithmTest。
 */
public final class Murmur3Hash128 {

    /** 固定种子。算法标识字符串中同时记录该值。 */
    public static final int FIXED_SEED = 0x9747B28C;

    /** 全局唯一的哈希算法标识，写入草图序列化格式并参与合并兼容性检查。 */
    public static final String HASH_ID = "MURMUR3_X64_128_SEED9747B28C_H1";

    private static final long C1 = 0x87c37b91114253d5L;
    private static final long C2 = 0x4cf5ad432745937fL;

    private Murmur3Hash128() {
    }

    private static long rotl64(long x, int r) {
        return (x << r) | (x >>> (64 - r));
    }

    private static long fmix64(long k) {
        k ^= k >>> 33;
        k *= 0xff51afd7ed558ccdL;
        k ^= k >>> 33;
        k *= 0xc4ceb9fe1a85ec53L;
        k ^= k >>> 33;
        return k;
    }

    /**
     * 计算 128 位哈希。
     *
     * @param data 输入字节，不为 null
     * @return 长度 2 的数组 {h1, h2}；HLL 仅消费 h1
     */
    public static long[] hash128(byte[] data) {
        return hash128(data, FIXED_SEED);
    }

    /** 按 UTF-8 编码后计算，返回 HLL 实际使用的 64 位 h1。 */
    public static long hashStringToH1(String s) {
        return hash128(s.getBytes(StandardCharsets.UTF_8), FIXED_SEED)[0];
    }

    /** 指定种子的完整实现（种子参数仅用于与参考实现保持一致的结构，生产路径固定）。 */
    public static long[] hash128(byte[] data, int seed) {
        long h1 = seed & 0xffffffffL;
        long h2 = seed & 0xffffffffL;

        int length = data.length;
        int nblocks = length / 16;

        for (int i = 0; i < nblocks; i++) {
            int base = i * 16;
            long k1 = littleEndianGetLong(data, base);
            long k2 = littleEndianGetLong(data, base + 8);

            k1 *= C1;
            k1 = rotl64(k1, 31);
            k1 *= C2;
            h1 ^= k1;

            h1 = rotl64(h1, 27);
            h1 += h2;
            h1 = h1 * 5 + 0x52dce729L;

            k2 *= C2;
            k2 = rotl64(k2, 33);
            k2 *= C1;
            h2 ^= k2;

            h2 = rotl64(h2, 31);
            h2 += h1;
            h2 = h2 * 5 + 0x38495ab5L;
        }

        long k1 = 0;
        long k2 = 0;
        int tailStart = nblocks * 16;

        switch (length - tailStart) {
            case 15:
                k2 ^= (long) (data[tailStart + 14] & 0xff) << 48;
                //$FALL-THROUGH$
            case 14:
                k2 ^= (long) (data[tailStart + 13] & 0xff) << 40;
                //$FALL-THROUGH$
            case 13:
                k2 ^= (long) (data[tailStart + 12] & 0xff) << 32;
                //$FALL-THROUGH$
            case 12:
                k2 ^= (long) (data[tailStart + 11] & 0xff) << 24;
                //$FALL-THROUGH$
            case 11:
                k2 ^= (long) (data[tailStart + 10] & 0xff) << 16;
                //$FALL-THROUGH$
            case 10:
                k2 ^= (long) (data[tailStart + 9] & 0xff) << 8;
                //$FALL-THROUGH$
            case 9:
                k2 ^= data[tailStart + 8] & 0xff;
                k2 *= C2;
                k2 = rotl64(k2, 33);
                k2 *= C1;
                h2 ^= k2;
                //$FALL-THROUGH$
            case 8:
                k1 ^= (long) (data[tailStart + 7] & 0xff) << 56;
                //$FALL-THROUGH$
            case 7:
                k1 ^= (long) (data[tailStart + 6] & 0xff) << 48;
                //$FALL-THROUGH$
            case 6:
                k1 ^= (long) (data[tailStart + 5] & 0xff) << 40;
                //$FALL-THROUGH$
            case 5:
                k1 ^= (long) (data[tailStart + 4] & 0xff) << 32;
                //$FALL-THROUGH$
            case 4:
                k1 ^= (long) (data[tailStart + 3] & 0xff) << 24;
                //$FALL-THROUGH$
            case 3:
                k1 ^= (long) (data[tailStart + 2] & 0xff) << 16;
                //$FALL-THROUGH$
            case 2:
                k1 ^= (long) (data[tailStart + 1] & 0xff) << 8;
                //$FALL-THROUGH$
            case 1:
                k1 ^= data[tailStart] & 0xff;
                k1 *= C1;
                k1 = rotl64(k1, 31);
                k1 *= C2;
                h1 ^= k1;
                //$FALL-THROUGH$
            default:
                break;
        }

        h1 ^= length;
        h2 ^= length;

        h1 += h2;
        h2 += h1;

        h1 = fmix64(h1);
        h2 = fmix64(h2);

        h1 += h2;
        h2 += h1;

        return new long[] {h1, h2};
    }

    private static long littleEndianGetLong(byte[] b, int off) {
        return (b[off] & 0xffL)
                | ((b[off + 1] & 0xffL) << 8)
                | ((b[off + 2] & 0xffL) << 16)
                | ((b[off + 3] & 0xffL) << 24)
                | ((b[off + 4] & 0xffL) << 32)
                | ((b[off + 5] & 0xffL) << 40)
                | ((b[off + 6] & 0xffL) << 48)
                | ((b[off + 7] & 0xffL) << 56);
    }
}
