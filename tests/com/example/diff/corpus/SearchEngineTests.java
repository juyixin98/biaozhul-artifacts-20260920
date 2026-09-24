package com.example.diff.corpus;

import com.example.diff.TestFramework;

import java.util.List;

/** Tests for local retrieval over the synthetic corpus. */
public final class SearchEngineTests {

    private SearchEngineTests() {
    }

    public static void register(TestFramework tf) {
        tf.test("finds docs by term and returns positions", SearchEngineTests::basicTerm);
        tf.test("AND semantics: missing term excludes doc", SearchEngineTests::andSemantics);
        tf.test("CRLF doc is searchable on bare words", SearchEngineTests::crlfSearch);
        tf.test("empty doc never matches", SearchEngineTests::emptyDoc);
        tf.test("title match boosts ranking", SearchEngineTests::titleBoost);
        tf.test("empty query returns no hits", SearchEngineTests::emptyQuery);
        tf.test("limit truncates results", SearchEngineTests::limitWorks);
    }

    private static SearchEngine engine() {
        return new SearchEngine(Corpus.synthetic());
    }

    private static void basicTerm() {
        List<SearchEngine.Hit> hits = engine().search("compilation", 10);
        TestFramework.assertTrue(hits.size() >= 2, "both build logs mention compilation");
        SearchEngine.Hit first = hits.get(0);
        TestFramework.assertTrue(first.matchedLines.size() > 0, "must report matched lines");
        TestFramework.assertTrue(first.matchedOffsets.size() > 0, "must report char offsets");
    }

    private static void andSemantics() {
        List<SearchEngine.Hit> hits = engine().search("compilation nonexistentword", 10);
        TestFramework.assertEquals(0, hits.size(), "AND query with absent term yields nothing");
    }

    private static void crlfSearch() {
        List<SearchEngine.Hit> hits = engine().search("worker", 10);
        boolean foundBeta = false;
        for (SearchEngine.Hit h : hits) {
            if ("log-002".equals(h.docId)) {
                foundBeta = true;
            }
        }
        TestFramework.assertTrue(foundBeta, "CRLF document must be found by word search");
    }

    private static void emptyDoc() {
        List<SearchEngine.Hit> hits = engine().search("anything", 10);
        for (SearchEngine.Hit h : hits) {
            TestFramework.assertFalse("empty-001".equals(h.docId), "empty doc cannot match");
        }
    }

    private static void titleBoost() {
        // "config" appears in titles of cfg-001/cfg-002; those should outrank docs
        // where the word only appears in the body (it doesn't anywhere else).
        List<SearchEngine.Hit> hits = engine().search("config", 10);
        TestFramework.assertTrue(hits.size() >= 2, "expected config docs");
        TestFramework.assertEquals("cfg-001", hits.get(0).docId);
    }

    private static void emptyQuery() {
        TestFramework.assertEquals(0, engine().search("", 10).size());
    }

    private static void limitWorks() {
        List<SearchEngine.Hit> all = engine().search("step", 100);
        List<SearchEngine.Hit> limited = engine().search("step", 1);
        TestFramework.assertTrue(all.size() > 1, "precondition: multiple matches");
        TestFramework.assertEquals(1, limited.size());
    }
}
