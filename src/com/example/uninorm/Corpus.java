package com.example.uninorm;

import java.util.List;

/**
 * Self-built synthetic corpus (no external data). The documents are crafted
 * to exercise every normalization feature under test: precomposed vs.
 * decomposed accents, case-fold expansions (ß), compatibility characters
 * (ligatures, full-width, roman numerals, ™), non-Latin scripts, and
 * supplementary-plane (surrogate-pair) characters.
 *
 * <p>Sequences that must survive the editor/compiler byte-for-byte are
 * written with explicit {@code \\uXXXX} escapes.
 */
public final class Corpus {

    private Corpus() {
    }

    public static List<Document> documents() {
        return List.of(
                new Document("doc-french",
                        "Café culture: the café serves café au lait. "
                                + "CAFÉ and café are the same word."),
                new Document("doc-german",
                        "Straße is German for street. "
                                + "STRASSE and strasse both mean Straße."),
                new Document("doc-ligature",
                        "The ﬁle and the ﬂag: ﬁnding ﬁsh "
                                + "needs the ﬁ ligature expanded."),
                new Document("doc-greek",
                        "Σίσυφος pushed the rock. "
                                + "σίσυφος and ΣΊΣΥΦΟΣ refer to Sisyphus."),
                new Document("doc-russian",
                        "Москва is the capital. москва and МОСКВА "
                                + "are one city."),
                new Document("doc-turkish",
                        "İSTANBUL is a large city. "
                                + "The dotted İ folds to i plus a combining dot."),
                new Document("doc-cjk-fullwidth",
                        "ＡＢＣ１２３ full-width text. "
                                + "Ｈｅｌｌｏ　Ｗｏｒｌｄ uses full-width letters."),
                new Document("doc-symbols",
                        "Ⅳ is a roman numeral. ™ marks a brand. "
                                + "① is a circled digit. ㌀ is a square katakana."),
                new Document("doc-emoji-math",
                        "Emoji 😀 and 👍🏽 plus math 𝐀𝐁𝐂 bold "
                                + "and 𝟜 double-struck digit."));
    }
}
