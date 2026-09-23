package phj.join;

import phj.core.Value;

/**
 * 分区哈希：基于单条 Value 的 64 位混合（fmix64 风格），
 * 配合每级不同种子，使热点键在递归拆分时换一种打散方式
 * （单列热点时打散仍无效，引擎会据此识别并直接进入有界回退）。
 */
public final class HashMix {

    private HashMix() {}

    public static long hashValue(Value v, long seed) {
        long h;
        switch (v.type) {
            case NULL -> h = 0L;
            case BOOLEAN -> h = v.asBool() ? 1L : 2L;
            case STRING -> h = stringHash(v.asString());
            case LONG -> h = v.asLong();
            case DOUBLE -> {
                double d = v.asDouble();
                h = (d == 0.0) ? 0L : Double.doubleToLongBits(d);
            }
            default -> throw new IllegalStateException();
        }
        return mix(h ^ seed);
    }

    /** 多列键的混合哈希（各列顺序敏感）。 */
    public static long hashKey(java.util.List<Value> cols, long seed) {
        long h = seed;
        for (Value c : cols) {
            h ^= hashValue(c, 0x9e3779b97f4a7c15L);
            h = mix(h);
        }
        return h;
    }

    private static long stringHash(String s) {
        // FNV-1a 64
        long h = 0xcbf29ce484222325L;
        for (int i = 0; i < s.length(); i++) {
            h ^= s.charAt(i);
            h *= 0x100000001b3L;
        }
        return h;
    }

    /** MurmurHash3 fmix64。 */
    public static long mix(long k) {
        k ^= k >>> 33;
        k *= 0xff51afd7ed558ccdL;
        k ^= k >>> 33;
        k *= 0xc4ceb9fe1a85ec53L;
        k ^= k >>> 33;
        return k;
    }

    /** 非负取模。 */
    public static int partition(long h, int n) {
        int m = (int) (h % n);
        return m < 0 ? m + n : m;
    }
}
