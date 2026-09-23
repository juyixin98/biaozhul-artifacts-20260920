package com.example.uninorm;

import java.nio.charset.StandardCharsets;
import java.util.Random;

import static com.example.uninorm.TestSupport.assertEquals;
import static com.example.uninorm.TestSupport.assertTrue;

/**
 * Property-based checks with a fixed seed. Builds random strings from an
 * alphabet of normalization pitfalls and verifies, for every code-point
 * aligned subrange of the normalized key, that the mapped original range
 * never cuts a code point in either UTF-16 or UTF-8 coordinates.
 */
public final class FuzzBoundaryTest {

    private FuzzBoundaryTest() {
    }

    /** Pitfalls: marks, case expansions, compatibility cps, supplementary. */
    private static final int[] ALPHABET = {
            'a', 'B', 'z', ' ',
            0x00DF,   // ß -> ss
            0x00E9,   // é -> e + combining acute
            0x0301,   // combining acute
            0x0307,   // combining dot above
            0xFB01,   // ﬁ -> fi
            0xFF21,   // full-width Ａ
            0xFF11,   // full-width １
            0x2163,   // Ⅳ -> iv
            0x2122,   // ™ -> tm
            0x0130,   // İ -> i + dot
            0x03C2,   // final sigma
            0x03A3,   // capital sigma
            0x03AF,   // Greek ί
            0x4E2D,   // CJK 中
            0x1F600,  // 😀 (supplementary, no fold)
            0x1F44D,  // 👍
            0x1F3FD,  // 🏽 skin tone modifier
            0x1D400,  // 𝐀 (supplementary -> a)
            0x1D7DC,  // 𝟜 (supplementary -> 4)
            0x2460,   // ① -> 1
    };

    private static final int SAMPLES = 3000;
    private static final int MAX_LEN = 40;

    public static void allRangesRespectCodePointBoundaries() {
        Random rnd = new Random(12809242026L);
        int subrangesChecked = 0;

        for (int sample = 0; sample < SAMPLES; sample++) {
            String input = randomString(rnd);
            NormalizedText nt = TextNormalizer.normalize(input);
            String norm = nt.normalized();
            int cpCount = input.codePointCount(0, input.length());
            int utf8Total = NormalizedText.utf8ByteLength(input);

            // Whole normalized key maps to the whole original string.
            OffsetRange whole = nt.mapRange(0, norm.length());
            assertEquals(0, whole.startUtf16(), sample + ": whole utf16 start");
            assertEquals(input.length(), whole.endUtf16(),
                    sample + ": whole utf16 end");
            assertEquals(0, whole.startCodePoint(), sample + ": whole cp start");
            assertEquals(cpCount, whole.endCodePoint(),
                    sample + ": whole cp end");
            assertEquals(0, whole.startUtf8(), sample + ": whole utf8 start");
            assertEquals(utf8Total, whole.endUtf8(),
                    sample + ": whole utf8 end");

            // Idempotence.
            assertEquals(norm, TextNormalizer.normalize(norm).normalized(),
                    sample + ": pipeline idempotent");

            // Every code-point aligned subrange of the normalized key.
            int[] starts = codePointStarts(norm);
            int n = starts.length;
            for (int i = 0; i < n; i++) {
                for (int j = i + 1; j <= n; j++) {
                    int ns = starts[i];
                    int ne = (j < n) ? starts[j] : norm.length();
                    OffsetRange r = nt.mapRange(ns, ne);
                    checkRange(sample, input, r);
                    subrangesChecked++;
                }
            }
        }
        assertTrue(subrangesChecked > 100_000,
                "must exercise a large number of subranges, got "
                        + subrangesChecked);
    }

    /** The normalized code-point runs appear in the same order as source. */
    public static void mappingIsMonotonic() {
        Random rnd = new Random(42L);
        for (int sample = 0; sample < 1000; sample++) {
            String input = randomString(rnd);
            NormalizedText nt = TextNormalizer.normalize(input);
            String norm = nt.normalized();
            int prevCp = -1;
            for (int pos = 0; pos < norm.length(); ) {
                int cp = norm.codePointAt(pos);
                int charCount = Character.charCount(cp);
                OffsetRange r = nt.mapRange(pos, pos + charCount);
                assertTrue(r.startCodePoint() >= prevCp,
                        sample + ": original cp indices non-decreasing");
                prevCp = r.startCodePoint();
                pos += charCount;
            }
        }
    }

    private static void checkRange(int sample, String input, OffsetRange r) {
        // Bounds inside the string.
        assertTrue(r.startUtf16() >= 0 && r.endUtf16() <= input.length()
                        && r.startUtf16() < r.endUtf16(),
                sample + ": utf16 span within bounds");
        assertTrue(r.startUtf8() >= 0 && r.endUtf8() <= NormalizedText
                        .utf8ByteLength(input) && r.startUtf8() < r.endUtf8(),
                sample + ": utf8 span within bounds");

        // UTF-16 boundaries are code-point boundaries.
        assertTrue(!Character.isLowSurrogate(input.charAt(r.startUtf16())),
                sample + ": start is a low surrogate -> code point cut");
        if (r.endUtf16() < input.length()) {
            assertTrue(!Character.isLowSurrogate(input.charAt(r.endUtf16())),
                    sample + ": end lands before a high surrogate pair tail");
        }
        assertTrue(r.endUtf16() == input.length()
                        || !Character.isHighSurrogate(
                        input.charAt(r.endUtf16() - 1)),
                sample + ": high surrogate just inside end -> pair cut");

        // Code-point coordinates agree.
        assertEquals(input.codePointCount(r.startUtf16(), r.endUtf16()),
                r.endCodePoint() - r.startCodePoint(),
                sample + ": code-point span");

        // UTF-8 coordinates agree with the actual encoding of the excerpt.
        String excerpt = input.substring(r.startUtf16(), r.endUtf16());
        assertEquals(excerpt.getBytes(StandardCharsets.UTF_8).length,
                r.endUtf8() - r.startUtf8(),
                sample + ": UTF-8 byte span");
        assertEquals(cumulativeUtf8(input, r.startUtf16()), r.startUtf8(),
                sample + ": UTF-8 start offset");

        // Every code point in the excerpt is complete: the first char is never
        // a lone trailing surrogate and the last char never a lone leading one
        // (already covered, double-checked via surrogate count parity).
        int highs = 0;
        int lows = 0;
        for (int k = r.startUtf16(); k < r.endUtf16(); k++) {
            char c = excerpt.charAt(k - r.startUtf16());
            if (Character.isHighSurrogate(c)) {
                highs++;
            }
            if (Character.isLowSurrogate(c)) {
                lows++;
            }
        }
        assertEquals(highs, lows,
                sample + ": unmatched surrogate inside mapped span");
    }

    /** UTF-8 byte length of input[0..utf16Pos), computed independently. */
    private static int cumulativeUtf8(String s, int utf16Pos) {
        int bytes = 0;
        for (int pos = 0; pos < utf16Pos; ) {
            int cp = s.codePointAt(pos);
            bytes += TextNormalizer.utf8ByteLength(cp);
            pos += Character.charCount(cp);
        }
        return bytes;
    }

    private static int[] codePointStarts(String s) {
        int[] starts = new int[s.codePointCount(0, s.length())];
        int idx = 0;
        for (int pos = 0; pos < s.length(); idx++) {
            starts[idx] = pos;
            pos += Character.charCount(s.codePointAt(pos));
        }
        return starts;
    }

    private static String randomString(Random rnd) {
        int len = 1 + rnd.nextInt(MAX_LEN);
        int[] cps = new int[len];
        for (int i = 0; i < len; i++) {
            cps[i] = ALPHABET[rnd.nextInt(ALPHABET.length)];
        }
        return new String(cps, 0, cps.length);
    }
}
