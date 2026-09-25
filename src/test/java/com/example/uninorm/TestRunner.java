package com.example.uninorm;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.util.Arrays;
import java.util.List;
import java.util.Map;

/**
 * Dependency-free test runner. Run: java -cp build/test com.example.uninorm.TestRunner
 * (working directory must be the project root, the HTTP test reads data/corpus.txt).
 * Exits non-zero if any test fails.
 *
 * All non-ASCII literals are written as unicode escapes so the tests do not depend
 * on source-file encoding or editor normalization.
 */
public final class TestRunner {

    // U+00E9 = \u00e9 composed, U+0301 = combining acute, U+00DF = \u00df, U+1E9E = \u1e9e,
    // U+FB00/01/02 = \ufb00 \ufb01 \ufb02, U+0130 = \u0130, U+0307 = combining dot above,
    // U+FF39 = fullwidth \uff39, U+1F600 = \ud83d\ude00
    static final String E_COMPOSED = "\u00e9";
    static final String E_DECOMPOSED = "e\u0301";
    static final String SHARP_S = "\u00df";
    static final String SHARP_S_CAP = "\u1e9e";
    static final String FI_LIG = "\ufb01";
    static final String FF_LIG = "\ufb00";
    static final String FL_LIG = "\ufb02";
    static final String I_DOT = "\u0130";
    static final String DOT_ABOVE = "\u0307";
    static final String FULLWIDTH_Y = "\uff39";
    static final String EMOJI = "\ud83d\ude00"; // surrogate pair
    static final String CJK = "\u65e5\u672c";

    static int passed = 0;
    static int failed = 0;

    public static void main(String[] args) throws Exception {
        testCombiningCharacters();
        testComposedVsDecomposedSearch();
        testCaseExpansionSharpS();
        testLigatureExpansion();
        testMultiByteCharacters();
        testMappingNeverCutsCodePoints();
        testMappingMonotonicAndInBounds();
        testCaseFoldingDottedI();
        testJsonRoundTrip();
        testHttpService();
        System.out.println();
        System.out.println("PASSED: " + passed + ", FAILED: " + failed);
        if (failed > 0) System.exit(1);
    }

    // ---- 1. combining characters: composed and decomposed normalize identically
    static void testCombiningCharacters() {
        String composed = "caf" + E_COMPOSED;
        String decomposed = "caf" + E_DECOMPOSED;
        check("composed != decomposed at source level", !composed.equals(decomposed));
        NormalizedText a = TextNormalizer.normalize(composed);
        NormalizedText b = TextNormalizer.normalize(decomposed);
        check("composed and decomposed normalize identically", a.text.equals(b.text));
        check("normalized \u00e9 is decomposed 'e' + U+0301",
                a.text.equals("cafe\u0301"));
    }

    // ---- 2. search: query in one form finds documents in the other form,
    //         and the hit range covers the FULL original character(s)
    static void testComposedVsDecomposedSearch() {
        SearchEngine engine = new SearchEngine();
        String doc = "Un cafe\u0301 ici"; // decomposed e-acute
        engine.addDocument("d1", doc);
        List<SearchEngine.Hit> hits = engine.search("caf\u00e9", 10); // composed query
        check("composed query finds decomposed document", hits.size() == 1);
        SearchEngine.Hit h = hits.get(0);
        check("hit covers full original 'cafe+U+0301'",
                h.matched.equals("cafe\u0301") && doc.substring(h.start, h.end).equals("cafe\u0301"));
        check("hit range is [3,8) in original UTF-16 offsets", h.start == 3 && h.end == 8);

        // reverse direction: decomposed query, composed document
        SearchEngine engine2 = new SearchEngine();
        String doc2 = "Le caf\u00e9 ouvert"; // composed
        engine2.addDocument("d2", doc2);
        List<SearchEngine.Hit> hits2 = engine2.search("cafe\u0301", 10);
        check("decomposed query finds composed document",
                hits2.size() == 1 && hits2.get(0).matched.equals("caf\u00e9"));
    }

    // ---- 3. case expansion: \u00df -> ss, hit must cover the whole \u00df
    static void testCaseExpansionSharpS() {
        check("\u00df folds to ss", TextNormalizer.normalize(SHARP_S).text.equals("ss"));
        check("\u1e9e folds to ss", TextNormalizer.normalize(SHARP_S_CAP).text.equals("ss"));
        check("STRASSE folds to strasse",
                TextNormalizer.normalize("STRASSE").text.equals("strasse"));

        SearchEngine engine = new SearchEngine();
        String doc = "Die Stra\u00dfe ist breit";
        engine.addDocument("g", doc);
        List<SearchEngine.Hit> hits = engine.search("STRASSE", 10);
        check("STRASSE finds Stra\u00dfe", hits.size() == 1);
        SearchEngine.Hit h = hits.get(0);
        check("hit covers full 'Stra\u00dfe' including \u00df", h.matched.equals("Stra\u00dfe"));
        check("hit range [4,10)", h.start == 4 && h.end == 10);

        // query "ss" alone must also expand to the full \u00df, never half of it
        List<SearchEngine.Hit> hits2 = engine.search("ss", 10);
        check("'ss' query hits \u00df", hits2.size() == 1);
        check("'ss' hit expands to whole \u00df", hits2.get(0).matched.equals("\u00df"));
    }

    // ---- 4. compatibility expansion: \ufb01 ligature -> fi
    static void testLigatureExpansion() {
        check("\ufb01 ligature decomposes to fi",
                TextNormalizer.normalize(FI_LIG).text.equals("fi"));
        SearchEngine engine = new SearchEngine();
        String doc = "The \ufb01le is \ufb01ne";
        engine.addDocument("l", doc);
        List<SearchEngine.Hit> hits = engine.search("fine", 10);
        check("'fine' finds '\ufb01ne' (ligature)", hits.size() == 1);
        check("hit covers the ligature", hits.get(0).matched.equals("\ufb01ne"));
    }

    // ---- 5. multi-byte characters: emoji (surrogate pairs) and CJK
    static void testMultiByteCharacters() {
        String doc = "a" + EMOJI + "b" + CJK + "c";
        SearchEngine engine = new SearchEngine();
        engine.addDocument("e", doc);

        List<SearchEngine.Hit> emoji = engine.search(EMOJI, 10);
        check("emoji found", emoji.size() == 1);
        SearchEngine.Hit h = emoji.get(0);
        check("emoji hit range covers both surrogate halves [1,3)",
                h.start == 1 && h.end == 3 && h.matched.equals(EMOJI));

        List<SearchEngine.Hit> cjk = engine.search(CJK, 10);
        check("CJK found", cjk.size() == 1 && cjk.get(0).matched.equals(CJK));

        // search for text adjacent to the emoji; range must not split the pair
        List<SearchEngine.Hit> around = engine.search("a" + EMOJI + "b", 10);
        check("spanning query found", around.size() == 1);
        check("spanning range [0,4)", around.get(0).start == 0 && around.get(0).end == 4);
    }

    // ---- 6. invariant: no mapped range ever cuts a code point
    static void testMappingNeverCutsCodePoints() {
        String[] samples = {
                "cafe\u0301 Stra\u00dfe " + EMOJI + " \u65e5\u672c \ufb01ne \u0130stanbul",
                EMOJI + EMOJI + EMOJI,
                "\u1e9e\u017f\u03c2 \ufb00 \ufb01 \ufb02",
                "a\u0301e\u0301i\u0301o\u0301u\u0301 x\u0302",
                "\uff28\uff45\uff4c\uff4c\uff4f\u3000\uff37\uff4f\uff52\uff4c\uff44", // fullwidth forms
        };
        String[] queries = {"cafe", "strasse", "ss", "fi", EMOJI, "\u65e5\u672c", "istanbul",
                "e", "u", "hello", "world", "x", "\ufb00"};
        for (String doc : samples) {
            SearchEngine engine = new SearchEngine();
            engine.addDocument("d", doc);
            for (String q : queries) {
                for (SearchEngine.Hit h : engine.search(q, 50)) {
                    check("range in bounds for query " + q,
                            h.start >= 0 && h.end <= doc.length() && h.start < h.end);
                    // start must not be a low surrogate (would split a pair)
                    check("start does not split surrogate pair (query=" + q + ")",
                            !Character.isLowSurrogate(doc.charAt(h.start)));
                    // end-1 must not be a high surrogate
                    check("end does not split surrogate pair (query=" + q + ")",
                            !Character.isHighSurrogate(doc.charAt(h.end - 1)));
                    // end must not land inside a combining sequence
                    if (h.end < doc.length()) {
                        int t = Character.getType(doc.codePointAt(h.end));
                        check("end does not cut a combining sequence (query=" + q + ")",
                                t != Character.NON_SPACING_MARK
                                        && t != Character.COMBINING_SPACING_MARK
                                        && t != Character.ENCLOSING_MARK);
                    }
                }
            }
        }
    }

    // ---- 7. invariant: mapping arrays are monotonic and in bounds
    static void testMappingMonotonicAndInBounds() {
        String doc = "\u00c0b" + EMOJI + SHARP_S + FI_LIG + "x\u0302" + FULLWIDTH_Y;
        NormalizedText nt = TextNormalizer.normalize(doc);
        check("mapping length matches normalized length",
                nt.origStart.length == nt.text.length() && nt.origEnd.length == nt.text.length());
        int prevStart = -1;
        boolean ok = true;
        for (int i = 0; i < nt.text.length(); i++) {
            if (nt.origStart[i] < prevStart) ok = false;
            if (nt.origStart[i] < 0 || nt.origEnd[i] > doc.length()
                    || nt.origStart[i] >= nt.origEnd[i]) ok = false;
            prevStart = nt.origStart[i];
        }
        check("mapping monotonic and in bounds", ok);
        // fullwidth \uff39 folds to ASCII y
        check("fullwidth \uff39 folds to y", nt.text.endsWith("y"));
    }

    // ---- 8. dotted capital I (U+0130) folds per Unicode CaseFolding to i + U+0307.
    //         Documented behavior: plain "istanbul" does NOT match "\u0130stanbul".
    static void testCaseFoldingDottedI() {
        check("\u0130 folds to i + U+0307",
                TextNormalizer.normalize(I_DOT).text.equals("i" + DOT_ABOVE));
        SearchEngine engine = new SearchEngine();
        engine.addDocument("t", "\u0130stanbul");
        List<SearchEngine.Hit> hits = engine.search("i\u0307stanbul", 10);
        check("i+U+0307 query finds \u0130stanbul", hits.size() == 1);
        check("hit covers full \u0130stanbul", hits.get(0).matched.equals("\u0130stanbul"));
        check("plain 'istanbul' does NOT match \u0130stanbul (documented)",
                engine.search("istanbul", 10).isEmpty());
    }

    // ---- 9. JSON round trip incl. unicode escapes and surrogate pairs
    static void testJsonRoundTrip() {
        Map<String, Object> m = Json.obj(
                "text", "caf\u00e9 " + EMOJI + " " + SHARP_S,
                "n", 42L,
                "list", Arrays.asList("a", 1L, true, null));
        String encoded = Json.encode(m);
        Object decoded = Json.parse(encoded);
        check("json round trip", m.equals(decoded));
        Object v = Json.parse("{\"e\":\"\u00e9\",\"emoji\":\"\ud83d\ude00\"}");
        check("json \\u escapes", v.toString().contains("\u00e9") && v.toString().contains(EMOJI));
    }

    // ---- 10. HTTP integration test against a live server on an ephemeral port
    static void testHttpService() throws Exception {
        Server server = new Server();
        Main.loadCorpus(server.engine(), java.nio.file.Path.of("data/corpus.txt"));
        int port = server.start(0);
        try {
            HttpClient client = HttpClient.newHttpClient();
            String base = "http://127.0.0.1:" + port;

            String health = get(client, base + "/health");
            check("GET /health", health.contains("\"ok\""));

            String normResp = post(client, base + "/normalize",
                    "{\"text\":\"Stra\u00dfe\"}");
            check("POST /normalize folds \u00df->ss", normResp.contains("strasse"));

            String searchResp = post(client, base + "/search", "{\"query\":\"STRASSE\"}");
            check("POST /search finds Stra\u00dfe in corpus", searchResp.contains("Stra\u00dfe"));

            // corpus doc:cafe holds caf\u00e9 composed, CAF\u00c9, caf\u00e9 decomposed -> 3 hits
            String cafeResp = post(client, base + "/search",
                    "{\"query\":\"caf\u00e9\"}");
            check("composed caf\u00e9 query finds all 3 corpus occurrences",
                    cafeResp.contains("\"hitCount\":3"));

            String emojiResp = post(client, base + "/search",
                    "{\"query\":\"\ud83d\ude00\"}");
            check("emoji search works over HTTP", emojiResp.contains(EMOJI));

            String addResp = post(client, base + "/documents",
                    "{\"id\":\"new\",\"text\":\"Gr\u00fc\u00df Gott, Stra\u00dfe!\"}");
            check("POST /documents indexes", addResp.contains("\"indexed\":\"new\""));
            String searchNew = post(client, base + "/search", "{\"query\":\"strasse\"}");
            check("new doc searchable", searchNew.contains("\"docId\":\"new\""));

            String bad = post(client, base + "/search", "{\"query\":123}");
            check("bad request rejected", bad.contains("\"error\""));
        } finally {
            server.stop();
        }
    }

    // ---- helpers ----

    static String get(HttpClient c, String url) throws Exception {
        return c.send(HttpRequest.newBuilder(URI.create(url)).GET().build(),
                HttpResponse.BodyHandlers.ofString()).body();
    }

    static String post(HttpClient c, String url, String json) throws Exception {
        return c.send(HttpRequest.newBuilder(URI.create(url))
                        .header("Content-Type", "application/json")
                        .POST(HttpRequest.BodyPublishers.ofString(json)).build(),
                HttpResponse.BodyHandlers.ofString()).body();
    }

    static void check(String name, boolean cond) {
        if (cond) {
            passed++;
            System.out.println("PASS: " + name);
        } else {
            failed++;
            System.out.println("FAIL: " + name);
        }
    }
}
