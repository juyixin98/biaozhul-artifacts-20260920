package approxheavy.hash;

import java.nio.charset.StandardCharsets;

/**
 * Deterministic, seedable hash family.
 *
 * <p>{@link #murmur3X64128} is the public-domain MurmurHash3 x64 128-bit mix
 * (Austin Appleby). The two 64-bit halves act as two independent hashes, and
 * additional row hashes are derived as h1 + i * h2 (Kirsch–Mitzenmacher),
 * which is why every sketch records its seed: merging sketches from different
 * seeds would silently corrupt the counters.
 */
public final class Hashing {
    private Hashing() {
    }

    /** Returns the two 64-bit halves of Murmur3 x64 128 for {@code key} with {@code seed}. */
    public static long[] murmur3X64128(String key, long seed) {
        return murmur3X64128(key.getBytes(StandardCharsets.UTF_8), seed);
    }

    /** Returns the two 64-bit halves of Murmur3 x64 128 for {@code data} with {@code seed}. */
    public static long[] murmur3X64128(byte[] data, long seed) {
        long h1 = seed;
        long h2 = seed;

        final long c1 = 0x87c37b91114253d5L;
        final long c2 = 0x4cf5ad432745937fL;

        int length = data.length;
        int rounded = length & ~15;

        for (int offset = 0; offset < rounded; offset += 16) {
            long k1 = littleEndianLong(data, offset);
            long k2 = littleEndianLong(data, offset + 8);

            k1 *= c1;
            k1 = Long.rotateLeft(k1, 31);
            k1 *= c2;
            h1 ^= k1;

            h1 = Long.rotateLeft(h1, 27);
            h1 += h2;
            h1 = h1 * 5 + 0x52dce729L;

            k2 *= c2;
            k2 = Long.rotateLeft(k2, 33);
            k2 *= c1;
            h2 ^= k2;

            h2 = Long.rotateLeft(h2, 31);
            h2 += h1;
            h2 = h2 * 5 + 0x38495ab5L;
        }

        long k1 = 0;
        long k2 = 0;
        int tail = length & 15;

        if (tail >= 15) {
            k2 ^= (data[rounded + 14] & 0xffL) << 48;
        }
        if (tail >= 14) {
            k2 ^= (data[rounded + 13] & 0xffL) << 40;
        }
        if (tail >= 13) {
            k2 ^= (data[rounded + 12] & 0xffL) << 32;
        }
        if (tail >= 12) {
            k2 ^= (data[rounded + 11] & 0xffL) << 24;
        }
        if (tail >= 11) {
            k2 ^= (data[rounded + 10] & 0xffL) << 16;
        }
        if (tail >= 10) {
            k2 ^= (data[rounded + 9] & 0xffL) << 8;
        }
        if (tail >= 9) {
            k2 ^= (data[rounded + 8] & 0xffL);
            k2 *= c2;
            k2 = Long.rotateLeft(k2, 33);
            k2 *= c1;
            h2 ^= k2;
        }

        if (tail >= 8) {
            k1 ^= (data[rounded + 7] & 0xffL) << 56;
        }
        if (tail >= 7) {
            k1 ^= (data[rounded + 6] & 0xffL) << 48;
        }
        if (tail >= 6) {
            k1 ^= (data[rounded + 5] & 0xffL) << 40;
        }
        if (tail >= 5) {
            k1 ^= (data[rounded + 4] & 0xffL) << 32;
        }
        if (tail >= 4) {
            k1 ^= (data[rounded + 3] & 0xffL) << 24;
        }
        if (tail >= 3) {
            k1 ^= (data[rounded + 2] & 0xffL) << 16;
        }
        if (tail >= 2) {
            k1 ^= (data[rounded + 1] & 0xffL) << 8;
        }
        if (tail >= 1) {
            k1 ^= (data[rounded] & 0xffL);
            k1 *= c1;
            k1 = Long.rotateLeft(k1, 31);
            k1 *= c2;
            h1 ^= k1;
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

    /** Hash for row {@code row} of a sketch with the given seed. */
    public static long rowHash(String key, long seed, int row) {
        long[] pair = murmur3X64128(key, seed);
        return pair[0] + (long) row * pair[1];
    }

    /** Non-negative bucket index for a 64-bit hash and the given width. */
    public static int bucket(long hash, int width) {
        return (int) (Math.floorMod(hash, (long) width - 1L) + 1L);
    }

    private static long littleEndianLong(byte[] b, int offset) {
        return (b[offset] & 0xffL)
                | ((b[offset + 1] & 0xffL) << 8)
                | ((b[offset + 2] & 0xffL) << 16)
                | ((b[offset + 3] & 0xffL) << 24)
                | ((b[offset + 4] & 0xffL) << 32)
                | ((b[offset + 5] & 0xffL) << 40)
                | ((b[offset + 6] & 0xffL) << 48)
                | ((b[offset + 7] & 0xffL) << 56);
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
