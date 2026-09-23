package hllengine.hll;

/**
 * MurmurHash3, {@code x64_128} variant, streaming-free byte-array
 * implementation. This is the one and only, frozen hash function used by the
 * engine: changing it would change every estimate and invalidate the merge
 * contract, so it is identified explicitly in every serialized sketch
 * ({@code hashId = "MURMUR3_X64_128"}).
 *
 * <p>Ported from the canonical public-domain reference implementation by
 * Austin Appleby (MurmurHash3.cpp, {@code MurmurHash3_x64_128}). Only the
 * first 64 bits ({@code h1}) are used by {@link HllSketch}, but {@link #hash128}
 * is exposed (and tested) so the implementation can be checked against the
 * published known-answer vectors.
 */
public final class MurmurHash3 {

    /** Stable identifier written into every sketch; merge requires equality. */
    public static final String HASH_ID = "MURMUR3_X64_128";

    private MurmurHash3() {
    }

    private static long getLittleEndianLong(byte[] data, int offset) {
        long v = 0;
        for (int k = 0; k < 8; k++) {
            v |= (data[offset + k] & 0xffL) << (8 * k);
        }
        return v;
    }

    private static long rotl64(long x, int r) {
        return (x << r) | (x >>> (64 - r));
    }

    /** Returns the first 64 bits ({@code h1}) of the 128-bit hash. */
    public static long hash64(byte[] key, int seed) {
        return hash128(key, seed)[0];
    }

    /**
     * Returns {@code (h1, h2)} of the {@code x64_128} hash. Do not modify the
     * returned array.
     */
    public static long[] hash128(byte[] key, int seed) {
        final long c1 = 0x87c37b91114253d5L;
        final long c2 = 0x4cf5ad432745937fL;

        long h1 = seed;
        long h2 = seed;
        int length = key.length;

        int body = length & ~15; // full 16-byte blocks

        for (int offset = 0; offset < body; offset += 16) {
            long k1 = getLittleEndianLong(key, offset);
            long k2 = getLittleEndianLong(key, offset + 8);

            k1 *= c1;
            k1 = rotl64(k1, 31);
            k1 *= c2;
            h1 ^= k1;
            h1 = rotl64(h1, 27);
            h1 += h2;
            h1 = h1 * 5 + 0x52dce729L;

            k2 *= c2;
            k2 = rotl64(k2, 33);
            k2 *= c1;
            h2 ^= k2;
            h2 = rotl64(h2, 31);
            h2 += h1;
            h2 = h2 * 5 + 0x38495ab5L;
        }

        long k1 = 0;
        long k2 = 0;
        int tail = length & 15;
        int base = body;

        switch (tail) {
            case 15: k2 ^= (key[base + 14] & 0xffL) << 48;
            case 14: k2 ^= (key[base + 13] & 0xffL) << 40;
            case 13: k2 ^= (key[base + 12] & 0xffL) << 32;
            case 12: k2 ^= (key[base + 11] & 0xffL) << 24;
            case 11: k2 ^= (key[base + 10] & 0xffL) << 16;
            case 10: k2 ^= (key[base + 9] & 0xffL) << 8;
            case 9:
                k2 ^= (key[base + 8] & 0xffL);
                k2 *= c2;
                k2 = rotl64(k2, 33);
                k2 *= c1;
                h2 ^= k2;
            case 8: k1 ^= (key[base + 7] & 0xffL) << 56;
            case 7: k1 ^= (key[base + 6] & 0xffL) << 48;
            case 6: k1 ^= (key[base + 5] & 0xffL) << 40;
            case 5: k1 ^= (key[base + 4] & 0xffL) << 32;
            case 4: k1 ^= (key[base + 3] & 0xffL) << 24;
            case 3: k1 ^= (key[base + 2] & 0xffL) << 16;
            case 2: k1 ^= (key[base + 1] & 0xffL) << 8;
            case 1:
                k1 ^= (key[base] & 0xffL);
                k1 *= c1;
                k1 = rotl64(k1, 31);
                k1 *= c2;
                h1 ^= k1;
            default:
                // no tail
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

    /** 64-bit finalizer from Murmur3. */
    public static long fmix64(long k) {
        k ^= k >>> 33;
        k *= 0xff51afd7ed558ccdL;
        k ^= k >>> 33;
        k *= 0xc4ceb9fe1a85ec53L;
        k ^= k >>> 33;
        return k;
    }
}
