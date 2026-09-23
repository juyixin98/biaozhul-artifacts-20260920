package com.example.uninorm;

import static com.example.uninorm.TestSupport.assertEquals;

/**
 * Normalization policy tests: NFKD decomposition, case-fold expansion,
 * compatibility characters, supplementary plane.
 *
 * <p>All non-ASCII strings are built from integer code points via
 * {@link #cps(int...)} so this source stays pure ASCII and cannot be altered
 * by editor/source-file normalization.
 */
public final class NormalizationTest {

    private NormalizationTest() {
    }

    /** Build a String from code points (e.g. {@code cps('C', 0x00E9)}). */
    static String cps(int... codePoints) {
        return new String(codePoints, 0, codePoints.length);
    }

    /** Precomposed é (U+00E9) and decomposed e + U+0301 produce one key. */
    public static void composedEqualsDecomposed() {
        NormalizedText nfc = TextNormalizer.normalize(cps('C', 'a', 'f', 0x00E9));
        NormalizedText nfd =
                TextNormalizer.normalize(cps('C', 'a', 'f', 'e', 0x0301));
        String expected = cps('c', 'a', 'f', 'e', 0x0301);
        assertEquals(expected, nfc.normalized(),
                "Café(NFC) must decompose to c a f e + U+0301");
        assertEquals(expected, nfd.normalized(),
                "Café(NFD) must decompose to the same key");
    }

    /** ß (U+00DF) expands to ss via the upper-then-lower fold. */
    public static void sharpSExpands() {
        assertEquals("strasse",
                TextNormalizer.normalize(cps('S', 't', 'r', 'a', 0x00DF, 'e'))
                        .normalized(),
                "Straße must fold to strasse");
        assertEquals("strasse",
                TextNormalizer.normalize("STRASSE").normalized(),
                "STRASSE must fold to strasse");
    }

    /** Compatibility characters folded by NFKD. */
    public static void compatibilityCharacters() {
        assertEquals("fi",
                TextNormalizer.normalize(cps(0xFB01)).normalized(),
                "fi-ligature U+FB01 must expand");
        assertEquals("fl",
                TextNormalizer.normalize(cps(0xFB02)).normalized(),
                "fl-ligature U+FB02 must expand");
        assertEquals("a",
                TextNormalizer.normalize(cps(0xFF21)).normalized(),
                "full-width A U+FF21 must fold to a");
        assertEquals("abc123",
                TextNormalizer.normalize(
                        cps(0xFF21, 0xFF22, 0xFF23, 0xFF11, 0xFF12, 0xFF13))
                        .normalized(),
                "full-width alphanumerics must fold");
        assertEquals("iv",
                TextNormalizer.normalize(cps(0x2163)).normalized(),
                "roman numeral IV U+2163 must fold to iv");
        assertEquals("tm",
                TextNormalizer.normalize(cps(0x2122)).normalized(),
                "trade mark sign U+2122 must fold to tm");
        assertEquals("1",
                TextNormalizer.normalize(cps(0x2460)).normalized(),
                "circled digit one U+2460 must fold to 1");
        // U+3300 SQUARE APAATO -> ア ハ ゜ー ト (NFKD also decomposes the
        // precomposed パ into ハ + U+309A combining handakuten)
        assertEquals(cps(0x30A2, 0x30CF, 0x309A, 0x30FC, 0x30C8),
                TextNormalizer.normalize(cps(0x3300)).normalized(),
                "square katakana word U+3300 must expand to decomposed kana");
    }

    /** Turkish dotted capital I (U+0130) -> i + U+0307 (documented trade-off). */
    public static void dottedCapitalI() {
        assertEquals(cps('i', 0x0307),
                TextNormalizer.normalize(cps(0x0130)).normalized(),
                "İ must fold to i + combining dot above");
    }

    /** Greek upper/lower forms meet (final-sigma exception documented). */
    public static void greekCaseFolding() {
        // Σ Ί Σ Υ Φ Ο Σ
        String upper = TextNormalizer.normalize(
                cps(0x03A3, 0x038A, 0x03A3, 0x03A5, 0x03A6, 0x039F, 0x03A3))
                .normalized();
        // σ ί σ υ φ ο ς
        String lower = TextNormalizer.normalize(
                cps(0x03C3, 0x03AF, 0x03C3, 0x03C5, 0x03C6, 0x03BF, 0x03C2))
                .normalized();
        assertEquals(upper, lower,
                "Greek upper and lower forms must normalize equally");
    }

    /** Bold mathematical letters (supplementary plane) fold to ASCII. */
    public static void mathematicalBold() {
        assertEquals("abc",
                TextNormalizer.normalize(cps(0x1D400, 0x1D401, 0x1D402))
                        .normalized(),
                "mathematical bold ABC must fold to abc");
        assertEquals("4",
                TextNormalizer.normalize(cps(0x1D7DC)).normalized(),
                "double-struck 4 U+1D7DC must fold to 4");
    }

    /** Emoji pass through unchanged. */
    public static void emojiPassThrough() {
        assertEquals(cps(0x1F600),
                TextNormalizer.normalize(cps(0x1F600)).normalized(),
                "grinning face must be unchanged");
        assertEquals(cps(0x1F44D, 0x1F3FD),
                TextNormalizer.normalize(cps(0x1F44D, 0x1F3FD)).normalized(),
                "thumbs up + skin tone modifier must be unchanged");
    }

    /** Applying the pipeline to its own output must be a fixed point. */
    public static void idempotentOnCorpus() {
        for (Document d : Corpus.documents()) {
            String once = TextNormalizer.normalize(d.text()).normalized();
            String twice = TextNormalizer.normalize(once).normalized();
            assertEquals(once, twice,
                    "pipeline must be idempotent for " + d.id());
        }
    }

    /** Empty and ASCII input edge cases. */
    public static void edgeCases() {
        assertEquals("", TextNormalizer.normalize("").normalized(),
                "empty string normalizes to empty");
        assertEquals("hello world",
                TextNormalizer.normalize("Hello World").normalized(),
                "plain ASCII only changes case");
    }
}
