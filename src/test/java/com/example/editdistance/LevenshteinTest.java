package com.example.editdistance;

import org.junit.jupiter.api.Test;

import java.nio.charset.StandardCharsets;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

class LevenshteinTest {

    @Test
    void knownDistances() {
        assertEquals(0, Levenshtein.distance("kitten", "kitten"));
        assertEquals(3, Levenshtein.distance("kitten", "sitting"));
        assertEquals(2, Levenshtein.distance("flaw", "lawn"));
        assertEquals(1, Levenshtein.distance("abc", "abd"));
        assertEquals(1, Levenshtein.distance("abc", "aec"));
    }

    @Test
    void emptyStrings() {
        assertEquals(0, Levenshtein.distance("", ""));
        assertEquals(3, Levenshtein.distance("", "abc"));
        assertEquals(3, Levenshtein.distance("abc", ""));
    }

    @Test
    void countsCodePointsNotBytes() {
        // "é" is 2 bytes in UTF-8 but 1 code point: distance to "e" must be 1, not 2.
        assertEquals(2, "é".getBytes(StandardCharsets.UTF_8).length);
        assertEquals(1, Levenshtein.distance("é", "e"));
        // CJK characters are 3 bytes each but 1 code point each.
        assertEquals(1, Levenshtein.distance("编辑", "编纂"));
    }

    @Test
    void countsCodePointsNotUtf16Chars() {
        // Emoji are surrogate pairs in UTF-16 (2 chars) but 1 code point.
        assertEquals(2, "😀".length());
        assertEquals(1, "😀".codePointCount(0, 2));
        assertEquals(1, Levenshtein.distance("😀", "😃"));
        assertEquals(1, Levenshtein.distance("a😀b", "a😃b"));
        assertEquals(0, Levenshtein.distance("😀😃", "😀😃"));
        assertEquals(1, Levenshtein.distance("😀😃", "😀"));
    }

    @Test
    void combiningCharactersFollowNormalizationPolicy() {
        String nfc = "caf\u00E9";   // é as single code point U+00E9
        String nfd = "cafe\u0301";  // e + combining acute U+0301
        assertEquals(4, nfc.codePointCount(0, nfc.length()));
        assertEquals(5, nfd.codePointCount(0, nfd.length()));
        // Without normalization the two spellings differ (substitute é→e, insert ́).
        assertEquals(2, Levenshtein.distance(
                Normalize.NONE.apply(nfc), Normalize.NONE.apply(nfd)));
        // With NFC (the default policy) they are identical.
        assertEquals(0, Levenshtein.distance(Normalize.NFC.apply(nfc), Normalize.NFC.apply(nfd)));
    }

    @Test
    void longCommonPrefix() {
        String prefix = "a".repeat(500);
        String s = prefix + "x";
        String t = prefix + "y";
        assertEquals(1, Levenshtein.distance(s, t));
        assertEquals(0, Levenshtein.distance(prefix + "z", prefix + "z"));
        assertEquals(2, Levenshtein.distance(prefix, prefix + "zz"));
    }

    @Test
    void symmetricAndNonNegative() {
        String[] samples = {"", "a", "ab", "abc", "编辑距离", "😀🚀", "caf\u00E9", "cafe\u0301"};
        for (String a : samples) {
            for (String b : samples) {
                assertEquals(Levenshtein.distance(a, b), Levenshtein.distance(b, a));
                assertTrue(Levenshtein.distance(a, b) >= 0);
            }
        }
    }

    @Test
    void distanceWithinMatchesExactWhenBelowThreshold() {
        String[] samples = {"", "a", "kitten", "sitting", "编辑距离", "😀😃😀", "x".repeat(300)};
        for (String a : samples) {
            for (String b : samples) {
                int[] acps = a.codePoints().toArray();
                int[] bcps = b.codePoints().toArray();
                int exact = Levenshtein.distance(acps, bcps);
                for (int k = 0; k <= 4; k++) {
                    int bounded = Levenshtein.distanceWithin(acps, bcps, k);
                    if (exact <= k) {
                        assertEquals(exact, bounded, "a=" + a + " b=" + b + " k=" + k);
                    } else {
                        assertTrue(bounded > k, "a=" + a + " b=" + b + " k=" + k);
                    }
                }
            }
        }
    }
}
