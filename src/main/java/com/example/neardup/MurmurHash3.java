package com.example.neardup;

import java.nio.charset.StandardCharsets;

/**
 * MurmurHash3 x64 128-bit (Austin Appleby, public domain reference algorithm).
 * Used as the single, fixed base hash for shingles. Deterministic across JVM
 * runs and platforms: little-endian block reads, fixed seed.
 */
public final class MurmurHash3 {

    private static final long C1 = 0x87c37b91114253d5L;
    private static final long C2 = 0x4cf5ad432745937fL;

    private MurmurHash3() {
    }

    /** Returns the two 64-bit halves {h1, h2} of the 128-bit hash. */
    public static long[] hash128(String input, long seed) {
        byte[] key = input.getBytes(StandardCharsets.UTF_8);
        long h1 = seed;
        long h2 = seed;

        int nblocks = key.length / 16;
        for (int i = 0; i < nblocks; i++) {
            long k1 = getLongLittleEndian(key, i * 16);
            long k2 = getLongLittleEndian(key, i * 16 + 8);

            k1 *= C1;
            k1 = Long.rotateLeft(k1, 31);
            k1 *= C2;
            h1 ^= k1;

            h1 = Long.rotateLeft(h1, 27);
            h1 += h2;
            h1 = h1 * 5 + 0x52dce729L;

            k2 *= C2;
            k2 = Long.rotateLeft(k2, 33);
            k2 *= C1;
            h2 ^= k2;

            h2 = Long.rotateLeft(h2, 31);
            h2 += h1;
            h2 = h2 * 5 + 0x38495ab5L;
        }

        long k1 = 0;
        long k2 = 0;
        int tail = nblocks * 16;
        // Deliberate fall-through, as in the reference implementation.
        switch (key.length & 15) {
            case 15:
                k2 ^= (long) (key[tail + 14] & 0xff) << 48;
            case 14:
                k2 ^= (long) (key[tail + 13] & 0xff) << 40;
            case 13:
                k2 ^= (long) (key[tail + 12] & 0xff) << 32;
            case 12:
                k2 ^= (long) (key[tail + 11] & 0xff) << 24;
            case 11:
                k2 ^= (long) (key[tail + 10] & 0xff) << 16;
            case 10:
                k2 ^= (long) (key[tail + 9] & 0xff) << 8;
            case 9:
                k2 ^= (long) (key[tail + 8] & 0xff);
                k2 *= C2;
                k2 = Long.rotateLeft(k2, 33);
                k2 *= C1;
                h2 ^= k2;
            case 8:
                k1 ^= (long) (key[tail + 7] & 0xff) << 56;
            case 7:
                k1 ^= (long) (key[tail + 6] & 0xff) << 48;
            case 6:
                k1 ^= (long) (key[tail + 5] & 0xff) << 40;
            case 5:
                k1 ^= (long) (key[tail + 4] & 0xff) << 32;
            case 4:
                k1 ^= (long) (key[tail + 3] & 0xff) << 24;
            case 3:
                k1 ^= (long) (key[tail + 2] & 0xff) << 16;
            case 2:
                k1 ^= (long) (key[tail + 1] & 0xff) << 8;
            case 1:
                k1 ^= (long) (key[tail] & 0xff);
                k1 *= C1;
                k1 = Long.rotateLeft(k1, 31);
                k1 *= C2;
                h1 ^= k1;
            default:
                // no tail bytes
        }

        h1 ^= key.length;
        h2 ^= key.length;
        h1 += h2;
        h2 += h1;
        h1 = fmix64(h1);
        h2 = fmix64(h2);
        h1 += h2;
        h2 += h1;
        return new long[]{h1, h2};
    }

    private static long getLongLittleEndian(byte[] key, int offset) {
        return ((long) key[offset] & 0xff)
                | (((long) key[offset + 1] & 0xff) << 8)
                | (((long) key[offset + 2] & 0xff) << 16)
                | (((long) key[offset + 3] & 0xff) << 24)
                | (((long) key[offset + 4] & 0xff) << 32)
                | (((long) key[offset + 5] & 0xff) << 40)
                | (((long) key[offset + 6] & 0xff) << 48)
                | (((long) key[offset + 7] & 0xff) << 56);
    }

    private static long fmix64(long k) {
        k ^= k >>> 33;
        k *= 0xff51afd7ed558ccdL;
        k ^= k >>> 33;
        k *= 0xc4ceb9fe1a85ec53L;
        k ^= k >>> 33;
        return k;
    }
}
