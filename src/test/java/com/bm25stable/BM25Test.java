package com.bm25stable;

import com.bm25stable.TestFramework.TestCase;

import java.util.List;
import java.util.Map;

import static com.bm25stable.TestFramework.assertClose;
import static com.bm25stable.TestFramework.assertEquals;
import static com.bm25stable.TestFramework.assertTrue;

/**
 * BM25 打分测试。基准值由 Python 独立计算（math.log，同一公式）：
 * 语料 d1="a a a"（tf=3, dl=3），d2="a b"（tf=1, dl=2），N=2, avgdl=2.5：
 *   idf(a) = ln(1 + (2-2+0.5)/(2+0.5)) = 0.1823215567939546
 *   score(d1) = 0.2747311129771919
 *   score(d2) = 0.19856803215183175
 */
public final class BM25Test {

    private static final double EPS = 1e-12;

    @TestCase
    static void scoresMatchHandComputedReference() {
        SearchEngine engine = new SearchEngine();
        engine.upsert("d1", "a a a");
        engine.upsert("d2", "a b");

        SearchResult result = engine.search("a", 10, null);
        assertEquals(2, result.totalHits(), "total hits");
        assertEquals("d1", result.hits().get(0).docId(), "higher tf ranks first");
        assertEquals("d2", result.hits().get(1).docId(), "second hit");
        assertClose(0.2747311129771919, result.hits().get(0).score(), EPS, "d1 score");
        assertClose(0.19856803215183175, result.hits().get(1).score(), EPS, "d2 score");
    }

    @TestCase
    static void idfFormula() {
        assertClose(Math.log(1.0 + 0.5 / 2.5), BM25.idf(2, 2), EPS, "idf(2,2)");
        assertTrue(BM25.idf(10, 1) > BM25.idf(10, 5), "rarer term has higher idf");
        assertTrue(BM25.idf(2, 2) > 0, "idf stays non-negative");
    }

    @TestCase
    static void repeatedTermInDocumentIncreasesScore() {
        SearchEngine engine = new SearchEngine();
        engine.upsert("once", "apple banana orange grape melon");
        engine.upsert("thrice", "apple apple apple");
        SearchResult result = engine.search("apple", 10, null);
        assertEquals("thrice", result.hits().get(0).docId(), "higher tf wins");
        assertTrue(result.hits().get(0).score() > result.hits().get(1).score(),
                "tf=3 score strictly greater than tf=1");
    }

    @TestCase
    static void repeatedTermInQueryCountsOnce() {
        SearchEngine engine = new SearchEngine();
        engine.upsert("d1", "apple pie");
        SearchResult single = engine.search("apple", 10, null);
        SearchResult repeated = engine.search("apple apple apple", 10, null);
        assertEquals(1, repeated.totalHits(), "same hit set");
        assertClose(single.hits().get(0).score(), repeated.hits().get(0).score(), EPS,
                "query term dedup keeps score unchanged");
    }

    @TestCase
    static void emptyDocumentsNeverMatch() {
        SearchEngine engine = new SearchEngine();
        engine.upsert("empty", "");
        engine.upsert("punct", "!!! ???");
        engine.upsert("real", "apple");
        SearchResult result = engine.search("apple", 10, null);
        assertEquals(1, result.totalHits(), "only the real doc matches");
        assertEquals("real", result.hits().get(0).docId(), "hit id");
        // 空语料极端情况：所有文档都为空时检索不抛异常
        SearchEngine emptyEngine = new SearchEngine();
        emptyEngine.upsert("e1", "");
        SearchResult emptyResult = emptyEngine.search("apple", 10, null);
        assertEquals(0, emptyResult.totalHits(), "no hits in all-empty corpus");
    }

    @TestCase
    static void equalScoresBrokenByDocIdAscending() {
        SearchEngine engine = new SearchEngine();
        // 故意乱序写入，验证排序与写入顺序无关
        engine.upsert("dup-3", "cherry cherry");
        engine.upsert("dup-1", "cherry cherry");
        engine.upsert("dup-2", "cherry cherry");
        SearchResult result = engine.search("cherry", 10, null);
        assertEquals(3, result.totalHits(), "all three match");
        assertEquals(List.of("dup-1", "dup-2", "dup-3"),
                result.hits().stream().map(SearchHit::docId).toList(), "tie broken by docId asc");
        assertClose(result.hits().get(0).score(), result.hits().get(1).score(), EPS, "scores equal 1-2");
        assertClose(result.hits().get(1).score(), result.hits().get(2).score(), EPS, "scores equal 2-3");
    }

    @TestCase
    static void unknownTermYieldsEmptyPage() {
        SearchEngine engine = new SearchEngine();
        engine.upsert("d1", "apple");
        SearchResult result = engine.search("zzzz", 10, null);
        assertEquals(0, result.totalHits(), "no hits");
        assertTrue(result.hits().isEmpty(), "empty hit list");
        assertEquals(false, result.hasMore(), "no more pages");
        assertEquals(null, result.nextCursor(), "no cursor");
    }

    @TestCase
    static void multiTermQuerySumsScores() {
        SearchEngine engine = new SearchEngine();
        engine.upsert("both", "apple banana");
        engine.upsert("only-apple", "apple pie");
        SearchResult result = engine.search("apple banana", 10, null);
        assertEquals("both", result.hits().get(0).docId(), "doc matching both terms ranks first");
    }
}
