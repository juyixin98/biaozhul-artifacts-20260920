package approx.cms;

import java.nio.charset.StandardCharsets;
import java.util.Random;

/**
 * {@code depth} independent 64-bit hash functions over UTF-8 item bytes.
 *
 * <p>Each row mixes the item bytes with FNV-1a and a row-specific offset, then
 * applies the Murmur3 finalizer ({@code fmix64}). The mix is fully determined
 * by the construction seed, so sketches created with the same depth/seed hash
 * every item to the same bucket on every row &mdash; the precondition for
 * {@link CountMinSketch#merge(CountMinSketch)}.
 */
final class HashFamily {

    private static final long FNV_OFFSET = 0xcbf29ce484222325L;
    private static final long FNV_PRIME = 0x100000001b3L;

    private final int depth;
    private final long seed;
    private final long[] rowOffsets;

    HashFamily(int depth, long seed) {
        if (depth <= 0) {
            throw new IllegalArgumentException("depth must be positive");
        }
        this.depth = depth;
        this.seed = seed;
        this.rowOffsets = new long[depth];
        // SplitMix-style per-seed PRNG: independent, reproducible offsets per row.
        Random rnd = new Random(seed ^ 0x9E3779B97F4A7C15L);
        for (int i = 0; i < depth; i++) {
            rowOffsets[i] = rnd.nextLong();
        }
    }

    long seed() {
        return seed;
    }

    int depth() {
        return depth;
    }

    /** Raw 64-bit hash of {@code item} on row {@code row}. */
    long hash(String item, int row) {
        byte[] bytes = item.getBytes(StandardCharsets.UTF_8);
        long h = FNV_OFFSET ^ rowOffsets[row];
        for (byte b : bytes) {
            h ^= (b & 0xffL);
            h *= FNV_PRIME;
        }
        h ^= seed;
        return fmix64(h ^ Long.rotateLeft(h, 29) ^ row);
    }

    /** Sketch bucket index (0 &le; result &lt; {@code width}) of {@code item} on row {@code row}. */
    int bucket(String item, int row, int width) {
        long v = hash(item, row);
        int idx = (int) Math.floorMod(v, (long) width);
        return idx;
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
