package com.example.edcand;

/** 规范化策略：NFC/NFD/NFKC/大小写折叠的行为。 */
public final class NormalizationTest {

    private NormalizationTest() {
    }

    public static void register(TestRunner r) {
        r.add("norm: NFC merges NFD combining sequence", t -> {
            String nfc = TextNormalization.NFC.apply("café");
            t.eq(nfc, "café", "NFC of e+acute");
            t.eq(CodePoints.length(nfc), 4, "4 code points after NFC");
        });
        r.add("norm: NFD splits into base + combining mark", t -> {
            String nfd = TextNormalization.NFD.apply("café");
            t.eq(nfd, "café", "NFD of café");
            t.eq(CodePoints.length(nfd), 5, "5 code points after NFD");
        });
        r.add("norm: NFKC folds full-width and ligatures", t -> {
            t.eq(TextNormalization.NFKC.apply("ＡＢＣ"), "abc", "full-width ascii");
            t.eq(TextNormalization.NFKC.apply("ﬁnd"), "find", "ligature fi");
            t.eq(TextNormalization.NFKC.apply("Ⅲ"), "iii", "roman numeral compatibility");
        });
        r.add("norm: case folding applied in all modes except NONE", t -> {
            t.eq(TextNormalization.NFC.apply("HELLO"), "hello", "NFC lowercases");
            t.eq(TextNormalization.NONE.apply("HELLO"), "HELLO", "NONE keeps case");
        });
        r.add("norm: emoji unchanged by normalization, still 1 code point", t -> {
            for (TextNormalization n : TextNormalization.values()) {
                t.eq(CodePoints.length(n.apply("😀")), 1, "emoji 1 cp under " + n);
            }
        });
        r.add("norm: parse rejects unknown form", t -> {
            try {
                TextNormalization.parse("bogus");
                t.fail("expected exception");
            } catch (IllegalArgumentException expected) {
                // ok
            }
            t.eq(TextNormalization.parse(null), TextNormalization.NFC, "null defaults NFC");
            t.eq(TextNormalization.parse("nfd"), TextNormalization.NFD, "lowercase accepted");
        });
    }
}
