package com.example.uninorm;

import static com.example.uninorm.TestSupport.assertEquals;
import static com.example.uninorm.TestSupport.assertThrows;

/**
 * Reverse offset-mapping tests. Covers combining marks, case expansion,
 * supplementary-plane (surrogate-pair) code points and UTF-8 offsets.
 */
public final class OffsetMappingTest {

    private OffsetMappingTest() {
    }

    private static String cps(int... codePoints) {
        return new String(codePoints, 0, codePoints.length);
    }

    /** One source code point (é) expands to two normalized chars. */
    public static void precomposedExpansionPointsBack() {
        NormalizedText nt = TextNormalizer.normalize(cps(0x00E9));
        // normalized = e U+0301 (two UTF-16 chars), one original code point.
        assertEquals(2, nt.normalized().length(), "é expands to 2 chars");
        OffsetRange whole = nt.mapRange(0, 2);
        assertEquals(0, whole.startUtf16(), "utf16 start");
        assertEquals(1, whole.endUtf16(), "é occupies 1 UTF-16 code unit");
        assertEquals(0, whole.startCodePoint(), "cp start");
        assertEquals(1, whole.endCodePoint(), "cp end");
        assertEquals(0, whole.startUtf8(), "utf8 start");
        assertEquals(2, whole.endUtf8(), "é is 2 UTF-8 bytes");

        // Each half of the expansion still maps to the same single code point.
        OffsetRange base = nt.mapRange(0, 1);
        OffsetRange mark = nt.mapRange(1, 2);
        assertEquals(0, base.startCodePoint(), "base half cp start");
        assertEquals(1, base.endCodePoint(), "base half cp end");
        assertEquals(0, mark.startCodePoint(), "mark half cp start");
        assertEquals(1, mark.endCodePoint(), "mark half cp end");
        assertEquals(cps(0x00E9), nt.excerpt(whole), "excerpt is full é");
    }

    /** Decomposed input: combining mark is its own original code point. */
    public static void decomposedInputMarkOwnsOffsets() {
        NormalizedText nt = TextNormalizer.normalize(cps('e', 0x0301));
        // match only the mark in normalized space
        OffsetRange mark = nt.mapRange(1, 2);
        assertEquals(1, mark.startCodePoint(), "mark cp start");
        assertEquals(2, mark.endCodePoint(), "mark cp end");
        assertEquals(1, mark.startUtf16(), "mark utf16 start");
        assertEquals(2, mark.endUtf16(), "mark utf16 end");
        // match the full accented letter -> spans both original code points
        OffsetRange whole = nt.mapRange(0, 2);
        assertEquals(0, whole.startCodePoint(), "whole cp start");
        assertEquals(2, whole.endCodePoint(), "whole cp end");
    }

    /** Case expansion ß -> ss: two normalized chars map to one code point. */
    public static void caseExpansionPointsBack() {
        NormalizedText nt = TextNormalizer.normalize(
                cps('S', 't', 'r', 'a', 0x00DF, 'e'));
        assertEquals("strasse", nt.normalized(), "normalized form");
        // normalized positions 4..6 are the two 's' letters from ß.
        OffsetRange sharpS = nt.mapRange(4, 6);
        assertEquals(4, sharpS.startCodePoint(), "ß cp start");
        assertEquals(5, sharpS.endCodePoint(), "ß cp end");
        assertEquals(4, sharpS.startUtf16(), "ß utf16 start");
        assertEquals(5, sharpS.endUtf16(), "ß is 1 UTF-16 unit");
        assertEquals("ß", nt.excerpt(sharpS), "excerpt is ß");

        OffsetRange whole = nt.mapRange(0, 7);
        assertEquals(0, whole.startUtf16(), "whole utf16 start");
        assertEquals(6, whole.endUtf16(), "whole utf16 end");
        assertEquals(0, whole.startCodePoint(), "whole cp start");
        assertEquals(6, whole.endCodePoint(), "6 original code points");
    }

    /** Supplementary plane: range cannot be requested in the middle. */
    public static void surrogatePairCannotBeCut() {
        NormalizedText nt = TextNormalizer.normalize(cps(0x1F600));
        assertEquals(2, nt.normalized().length(), "emoji is 2 UTF-16 units");
        OffsetRange whole = nt.mapRange(0, 2);
        assertEquals(0, whole.startUtf16(), "utf16 start");
        assertEquals(2, whole.endUtf16(), "utf16 end spans the pair");
        assertEquals(0, whole.startCodePoint(), "cp start");
        assertEquals(1, whole.endCodePoint(), "cp end");
        assertEquals(0, whole.startUtf8(), "utf8 start");
        assertEquals(4, whole.endUtf8(), "emoji is 4 UTF-8 bytes");
        // Asking for either half of the surrogate pair is rejected.
        assertThrows(() -> nt.mapRange(0, 1),
                "half-open range ending after the leading surrogate");
        assertThrows(() -> nt.mapRange(1, 2),
                "range starting at the trailing surrogate");
    }

    /** A supplementary code point that FOLDS to one ASCII char (bold A). */
    public static void supplementaryFoldsToAscii() {
        NormalizedText nt = TextNormalizer.normalize(cps(0x1D400));
        assertEquals("a", nt.normalized(), "bold A folds to a");
        OffsetRange r = nt.mapRange(0, 1);
        assertEquals(0, r.startUtf16(), "utf16 start");
        assertEquals(2, r.endUtf16(), "one supplementary cp = 2 units");
        assertEquals(0, r.startUtf8(), "utf8 start");
        assertEquals(4, r.endUtf8(), "bold A is 4 UTF-8 bytes");
    }

    /** Mixed-width text: UTF-8 offsets accumulate correctly. */
    public static void mixedWidthUtf8Offsets() {
        // '中'(3 bytes) é(2 bytes) 😀(4 bytes)
        NormalizedText nt = TextNormalizer.normalize(
                cps(0x4E2D, 0x00E9, 0x1F600));
        OffsetRange whole = nt.mapRange(0, nt.normalized().length());
        assertEquals(0, whole.startUtf8(), "utf8 start");
        assertEquals(9, whole.endUtf8(), "3+2+4 UTF-8 bytes");

        // Only the emoji: original cp index 2.
        String normalized = nt.normalized();
        int emojiStart = normalized.indexOf(cps(0x1F600));
        OffsetRange emoji = nt.mapRange(emojiStart,
                emojiStart + cps(0x1F600).length());
        assertEquals(2, emoji.startCodePoint(), "emoji cp start");
        assertEquals(5, emoji.startUtf8(), "emoji utf8 start (3+2)");
        assertEquals(9, emoji.endUtf8(), "emoji utf8 end");
        assertEquals(2, emoji.startUtf16(),
                "emoji utf16 start (中 + é each 1 unit)");
        assertEquals(4, emoji.endUtf16(), "emoji utf16 end");
    }

    /** Bad arguments are rejected. */
    public static void invalidRanges() {
        NormalizedText nt = TextNormalizer.normalize("hello");
        assertThrows(() -> nt.mapRange(2, 2), "empty span");
        assertThrows(() -> nt.mapRange(-1, 2), "negative start");
        assertThrows(() -> nt.mapRange(0, 99), "end past length");
        assertThrows(() -> nt.mapRange(3, 2), "inverted span");
    }
}
