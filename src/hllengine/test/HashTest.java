package hllengine.test;

import hllengine.hll.MurmurHash3;

import java.nio.charset.StandardCharsets;

/**
 * Known-answer tests for the frozen hash.
 *
 * <p>The main check reproduces the verification harness from Austin Appleby's
 * reference MurmurHash3.cpp: hash 256 keys (key i is i bytes each equal to i,
 * seed 256-i), concatenate the 16-byte digests, hash that 4096-byte blob with
 * seed 0, and take the first little-endian uint32. The reference prints
 * {@code 0x6384BA69} for {@code MurmurHash3_x64_128}.
 */
public final class HashTest implements TestRunner.Suite {

    @Override
    public void register(TestRunner.Registry r) {
        r.add("hash.referenceVerificationVector0x6384BA69", this::verificationVector);
        r.add("hash.emptyStringIsAllZero", this::emptyKey);
        r.add("hash.deterministicAcrossCalls", this::deterministic);
        r.add("hash.seedChangesResult", this::seedSensitivity);
        r.add("hash.differentInputsDiffer", this::differentInputs);
    }

    private void verificationVector(TestRunner.Assert a) {
        byte[][] digests = new byte[256][16];
        for (int i = 0; i < 256; i++) {
            // SMHasher KeysetTest reuses one cumulative key buffer: after
            // setting key[i] = i it hashes the first i bytes, i.e. key i is
            // {0,1,...,i-1} with seed (256-i). See KeysetTest.cpp.
            byte[] key = new byte[i];
            for (int k = 0; k < i; k++) key[k] = (byte) k;
            long[] h = MurmurHash3.hash128(key, 256 - i);
            // Store the digest in the reference's native little-endian layout.
            for (int half = 0; half < 2; half++) {
                long v = h[half];
                for (int b = 0; b < 8; b++) {
                    digests[i][half * 8 + b] = (byte) (v >>> (8 * b));
                }
            }
        }
        byte[] blob = new byte[256 * 16];
        for (int i = 0; i < 256; i++) {
            // The reference verification harness appends each 128-bit digest
            // in the machine's native little-endian byte order (h1 then h2).
            System.arraycopy(digests[i], 0, blob, i * 16, 16);
        }
        long[] finalHash = MurmurHash3.hash128(blob, 0);
        // The reference reads back the first 4 bytes of the digest and
        // interprets them as a little-endian uint32.
        byte h1b0 = (byte) finalHash[0];
        byte h1b1 = (byte) (finalHash[0] >>> 8);
        byte h1b2 = (byte) (finalHash[0] >>> 16);
        byte h1b3 = (byte) (finalHash[0] >>> 24);
        int verification = ((h1b0 & 0xff))
                | ((h1b1 & 0xff) << 8)
                | ((h1b2 & 0xff) << 16)
                | ((h1b3 & 0xff) << 24);
        a.eq(String.format("0x%08X", verification), "0x6384BA69",
                "MurmurHash3_x64_128 reference verification constant");
    }

    private void emptyKey(TestRunner.Assert a) {
        long[] h = MurmurHash3.hash128(new byte[0], 0);
        a.eq(h[0], 0L, "empty key h1");
        a.eq(h[1], 0L, "empty key h2");
    }

    private void deterministic(TestRunner.Assert a) {
        byte[] key = "mergeable-approx-dedup".getBytes(StandardCharsets.UTF_8);
        long first = MurmurHash3.hash64(key, 0);
        for (int k = 0; k < 100; k++) {
            a.eq(MurmurHash3.hash64(key, 0), first, "stable hash across calls");
        }
    }

    private void seedSensitivity(TestRunner.Assert a) {
        byte[] key = "user-123".getBytes(StandardCharsets.UTF_8);
        a.check(MurmurHash3.hash64(key, 0) != MurmurHash3.hash64(key, 1),
                "different seeds must produce different hashes");
    }

    private void differentInputs(TestRunner.Assert a) {
        // Type-tagging is done in ValueCoding; here raw distinct strings must differ.
        long h1 = MurmurHash3.hash64("1".getBytes(StandardCharsets.UTF_8), 0);
        long h2 = MurmurHash3.hash64("2".getBytes(StandardCharsets.UTF_8), 0);
        a.check(h1 != h2, "\"1\" vs \"2\" hashes differ");
        long h3 = MurmurHash3.hash64("12".getBytes(StandardCharsets.UTF_8), 0);
        long h4 = MurmurHash3.hash64("21".getBytes(StandardCharsets.UTF_8), 0);
        a.check(h3 != h4, "\"12\" vs \"21\" hashes differ");
    }
}
