package com.example.uninorm;

import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

import static com.example.uninorm.TestSupport.assertEquals;
import static com.example.uninorm.TestSupport.assertTrue;
import static com.example.uninorm.TestSupport.assertThrows;

/**
 * End-to-end searches on the synthetic corpus. Every hit is checked for
 * code-point/UTF-8 boundary safety, not just for textual equality.
 */
public final class SearchEngineTest {

    private SearchEngineTest() {
    }

    private static String cps(int... codePoints) {
        return new String(codePoints, 0, codePoints.length);
    }

    private static SearchEngine engine() {
        SearchEngine engine = new SearchEngine();
        for (Document d : Corpus.documents()) {
            engine.addDocument(d);
        }
        return engine;
    }

    private static final Map<String, String> TEXT_BY_ID = new HashMap<>();

    static {
        for (Document d : Corpus.documents()) {
            TEXT_BY_ID.put(d.id(), d.text());
        }
    }

    /** Core safety invariant applied to every hit in every test. */
    private static void verifyHit(SearchHit h) {
        String text = TEXT_BY_ID.get(h.docId());
        OffsetRange r = h.range();

        // UTF-16 boundaries sit on code points.
        if (r.startUtf16() > 0) {
            assertTrue(
                    !Character.isHighSurrogate(text.charAt(r.startUtf16() - 1)),
                    h.docId() + ": start must not orphan a high surrogate");
        }
        assertTrue(!Character.isLowSurrogate(text.charAt(r.startUtf16())),
                h.docId() + ": start must not be a low surrogate");
        if (r.endUtf16() < text.length()) {
            assertTrue(!Character.isLowSurrogate(text.charAt(r.endUtf16())),
                    h.docId() + ": end must not split a pair (low after end)");
        }
        assertTrue(
                r.endUtf16() == text.length()
                        || !Character.isHighSurrogate(text.charAt(r.endUtf16() - 1)),
                h.docId() + ": char before end must not be a high surrogate");

        // Code-point coordinates are consistent with UTF-16 coordinates.
        assertEquals(r.endCodePoint() - r.startCodePoint(),
                text.codePointCount(r.startUtf16(), r.endUtf16()),
                h.docId() + ": code-point count");

        // UTF-8 coordinates are consistent with the actual byte encoding.
        String excerpt = text.substring(r.startUtf16(), r.endUtf16());
        assertEquals(r.endUtf8() - r.startUtf8(),
                excerpt.getBytes(StandardCharsets.UTF_8).length,
                h.docId() + ": UTF-8 byte length");
        assertEquals(excerpt, h.matchedOriginal(),
                h.docId() + ": matchedOriginal must equal the original excerpt");

        // Excerpt re-normalizes to a form containing the normalized query
        // prefix/suffix structure: every excerpt is non-empty.
        assertTrue(!excerpt.isEmpty(), h.docId() + ": hit must be non-empty");
    }

    private static void verifyAll(List<SearchHit> hits) {
        for (SearchHit h : hits) {
            verifyHit(h);
        }
    }

    private static boolean hasHit(List<SearchHit> hits, String docId,
                                  String matched) {
        return hits.stream().anyMatch(h -> h.docId().equals(docId)
                && h.matchedOriginal().equals(matched));
    }

    /** ß / SS / ss all find Straße; every hit maps to full original spans. */
    public static void sharpS() {
        SearchEngine e = engine();
        List<SearchHit> hits = e.search("strasse", 0);
        verifyAll(hits);
        assertEquals(4, hits.size(),
                "Straße x2 + STRASSE + strasse = 4 hits in doc-german");
        for (SearchHit h : hits) {
            assertEquals("doc-german", h.docId(), "all hits in doc-german");
        }
        assertTrue(hasHit(hits, "doc-german",
                        cps('S', 't', 'r', 'a', 0x00DF, 'e')),
                "finds the precomposed Straße");
        assertTrue(hasHit(hits, "doc-german", "STRASSE"), "finds STRASSE");
        assertTrue(hasHit(hits, "doc-german", "strasse"), "finds strasse");

        // The ß hit: normalized length 7 collapses to 6 original UTF-16 units.
        SearchHit ssHit = hits.stream()
                .filter(h -> h.matchedOriginal().equals(
                        cps('S', 't', 'r', 'a', 0x00DF, 'e')))
                .findFirst().orElseThrow();
        assertEquals(6, ssHit.range().endUtf16() - ssHit.range().startUtf16(),
                "ß hit spans 6 UTF-16 units, not 7");
        assertEquals(6, ssHit.range().endCodePoint()
                - ssHit.range().startCodePoint(), "and 6 code points");

        // Case of the query is irrelevant.
        assertEquals(hits.size(), e.search("STRASSE", 0).size(),
                "uppercase query gives same number of hits");
    }

    /** Accented search across NFC/NFD spellings and mixed case. */
    public static void accentedCafe() {
        SearchEngine e = engine();
        List<SearchHit> hits = e.search(cps('c', 'a', 'f', 'e', 0x0301), 0);
        verifyAll(hits);
        assertEquals(5, hits.size(),
                "Café x3 + CAFÉ + decomposed café = 5 hits");
        // Uppercase precomposed query finds the same spans.
        List<SearchHit> upper = e.search(cps('C', 'A', 'F', 0x00C9), 0);
        verifyAll(upper);
        assertEquals(5, upper.size(), "uppercase query also finds 5 hits");
    }

    /** Ligature ﬁ expands to fi: "file" finds the precomposed word. */
    public static void ligatureExpansion() {
        SearchEngine e = engine();
        List<SearchHit> fileHits = e.search("file", 0);
        verifyAll(fileHits);
        assertTrue(hasHit(fileHits, "doc-ligature", cps(0xFB01, 'l', 'e')),
                "\"file\" finds ﬁle");
        SearchHit fileHit = fileHits.stream()
                .filter(h -> h.docId().equals("doc-ligature")).findFirst()
                .orElseThrow();
        assertEquals(3, fileHit.range().endUtf16() - fileHit.range().startUtf16(),
                "ﬁle is 3 UTF-16 units though the match key is 4 chars");

        List<SearchHit> findHits = e.search("find", 0);
        verifyAll(findHits);
        assertTrue(hasHit(findHits, "doc-ligature", cps(0xFB01, 'n', 'd')),
                "\"find\" finds the ﬁnd- preﬁx");
    }

    /** Full-width and bold alphanumerics match ASCII queries. */
    public static void fullWidthAndBold() {
        SearchEngine e = engine();
        List<SearchHit> fw = e.search("abc123", 0);
        verifyAll(fw);
        assertTrue(hasHit(fw, "doc-cjk-fullwidth",
                        cps(0xFF21, 0xFF22, 0xFF23, 0xFF11, 0xFF12, 0xFF13)),
                "full-width ＡＢＣ１２３ found by ascii query");

        List<SearchHit> abc = e.search("abc", 0);
        verifyAll(abc);
        assertTrue(hasHit(abc, "doc-emoji-math",
                        cps(0x1D400, 0x1D401, 0x1D402)),
                "bold 𝐀𝐁𝐂 found by \"abc\"");
        SearchHit boldHit = abc.stream()
                .filter(h -> h.docId().equals("doc-emoji-math"))
                .findFirst().orElseThrow();
        assertEquals(6, boldHit.range().endUtf16() - boldHit.range().startUtf16(),
                "bold ABC spans 6 UTF-16 units (3 surrogate pairs)");
        assertEquals(3, boldHit.range().endCodePoint()
                - boldHit.range().startCodePoint(), "and 3 code points");
        assertEquals(12, boldHit.range().endUtf8() - boldHit.range().startUtf8(),
                "and 12 UTF-8 bytes (4 each)");

        List<SearchHit> four = e.search("4", 0);
        verifyAll(four);
        assertTrue(hasHit(four, "doc-emoji-math", cps(0x1D7DC)),
                "double-struck 𝟜 found by \"4\"");
    }

    /** Symbols: roman numeral, TM, circled digit. */
    public static void symbols() {
        SearchEngine e = engine();
        List<SearchHit> tm = e.search("tm", 0);
        verifyAll(tm);
        assertTrue(hasHit(tm, "doc-symbols", cps(0x2122)), "™ found by tm");

        List<SearchHit> one = e.search("1", 0);
        verifyAll(one);
        assertTrue(hasHit(one, "doc-symbols", cps(0x2460)), "① found by 1");
        assertTrue(hasHit(one, "doc-cjk-fullwidth", cps(0xFF11)),
                "full-width １ found by 1");

        List<SearchHit> iv = e.search("iv", 0);
        verifyAll(iv);
        assertTrue(hasHit(iv, "doc-symbols", cps(0x2163)), "Ⅳ found by iv");
    }

    /** Greek upper/lower fold together. */
    public static void greek() {
        SearchEngine e = engine();
        // Σ Ί Σ Υ Φ Ο Σ
        String query = cps(0x03A3, 0x038A, 0x03A3, 0x03A5, 0x03A6, 0x039F,
                0x03A3);
        List<SearchHit> hits = e.search(query, 0);
        verifyAll(hits);
        // First occurrence in the corpus is the mixed-case Σίσυφος.
        assertTrue(hasHit(hits, "doc-greek",
                        cps(0x03A3, 0x03AF, 0x03C3, 0x03C5, 0x03C6, 0x03BF,
                                0x03C2)),
                "uppercase query finds Σίσυφος");
    }

    /** Cyrillic case folding. */
    public static void cyrillic() {
        SearchEngine e = engine();
        // м о с к в а (lowercase)
        String query = cps(0x043C, 0x043E, 0x0441, 0x043A, 0x0432, 0x0430);
        List<SearchHit> hits = e.search(query, 0);
        verifyAll(hits);
        assertEquals(3, hits.size(), "Москва / москва / МОСКВА = 3 hits");
        assertTrue(hasHit(hits, "doc-russian",
                        cps(0x041C, 0x043E, 0x0441, 0x043A, 0x0432, 0x0430)),
                "lowercase query finds capitalized Москва");
    }

    /** limit truncates; empty query yields nothing; null is rejected. */
    public static void limitAndEdgeCases() {
        SearchEngine e = engine();
        assertEquals(2, e.search(cps('c', 'a', 'f', 'e', 0x0301), 2).size(),
                "limit=2");
        assertEquals(0, e.search("", 0).size(), "empty query: no hits");
        assertThrows(() -> e.search(null, 0), "null query rejected");
    }
}
