package com.bm25stable;

import com.bm25stable.TestFramework.TestCase;

import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;

import static com.bm25stable.TestFramework.assertEquals;
import static com.bm25stable.TestFramework.assertFalse;
import static com.bm25stable.TestFramework.assertThrows;
import static com.bm25stable.TestFramework.assertTrue;

/** 分页行为测试：连续分页不漏不重、键集边界、游标校验。 */
public final class PaginationTest {

    /** 构造 7 篇命中文档：3 篇同分 + 其余分数各异，覆盖同分跨页场景。 */
    private static SearchEngine engineWithSevenHits() {
        SearchEngine engine = new SearchEngine();
        engine.upsert("dup-1", "term term");
        engine.upsert("dup-2", "term term");
        engine.upsert("dup-3", "term term");
        engine.upsert("solo-1", "term alpha");
        engine.upsert("solo-2", "term alpha beta");
        engine.upsert("solo-3", "term alpha beta gamma");
        engine.upsert("solo-4", "term alpha beta gamma delta");
        engine.upsert("noise-1", "unrelated words only");
        return engine;
    }

    @TestCase
    static void consecutivePagesHaveNoGapsOrDuplicates() {
        SearchEngine engine = engineWithSevenHits();
        int pageSize = 3;

        SearchResult page1 = engine.search("term", pageSize, null);
        assertEquals(7, page1.totalHits(), "total hits");
        assertEquals(3, page1.hits().size(), "page1 size");
        assertTrue(page1.hasMore(), "page1 hasMore");
        assertEquals(0, page1.offset(), "page1 offset");

        SearchResult page2 = engine.search("term", pageSize, page1.nextCursor());
        assertEquals(3, page2.hits().size(), "page2 size");
        assertTrue(page2.hasMore(), "page2 hasMore");
        assertEquals(3, page2.offset(), "page2 offset");

        SearchResult page3 = engine.search("term", pageSize, page2.nextCursor());
        assertEquals(1, page3.hits().size(), "page3 size");
        assertFalse(page3.hasMore(), "page3 hasMore");
        assertEquals(null, page3.nextCursor(), "page3 nextCursor null");

        List<String> allIds = new ArrayList<>();
        for (SearchResult r : List.of(page1, page2, page3)) {
            for (SearchHit h : r.hits()) {
                allIds.add(h.docId());
            }
        }
        assertEquals(7, allIds.size(), "collected size");
        Set<String> unique = new LinkedHashSet<>(allIds);
        assertEquals(7, unique.size(), "no duplicates across pages");
        assertEquals(Set.of("dup-1", "dup-2", "dup-3", "solo-1", "solo-2", "solo-3", "solo-4"),
                unique, "no missing docs across pages");
    }

    @TestCase
    static void tiedScoresAreStablyOrderedAcrossPageBoundary() {
        SearchEngine engine = engineWithSevenHits();
        // pageSize=2 使同分组 dup-1..3 跨越页边界（第 2、3 页之间）
        List<String> collected = new ArrayList<>();
        String cursor = null;
        int guard = 0;
        while (true) {
            SearchResult page = engine.search("term", 2, cursor);
            for (SearchHit h : page.hits()) {
                collected.add(h.docId());
            }
            if (!page.hasMore()) {
                break;
            }
            cursor = page.nextCursor();
            if (++guard > 20) {
                throw new AssertionError("pagination did not terminate");
            }
        }
        // 同分组必须按 docId 升序、连续出现
        int i1 = collected.indexOf("dup-1");
        int i2 = collected.indexOf("dup-2");
        int i3 = collected.indexOf("dup-3");
        assertTrue(i1 >= 0 && i2 == i1 + 1 && i3 == i2 + 1,
                "tied docs contiguous and ascending: " + collected);
    }

    @TestCase
    static void pageSequenceMatchesSingleBigPage() {
        SearchEngine engine = engineWithSevenHits();
        List<String> bigPage = engine.search("term", 100, null).hits()
                .stream().map(SearchHit::docId).toList();

        List<String> paged = new ArrayList<>();
        String cursor = null;
        while (true) {
            SearchResult page = engine.search("term", 3, cursor);
            page.hits().forEach(h -> paged.add(h.docId()));
            if (!page.hasMore()) {
                break;
            }
            cursor = page.nextCursor();
        }
        assertEquals(bigPage, paged, "paged sequence equals single big page");
    }

    @TestCase
    static void cursorBindsQueryAndPageSize() {
        SearchEngine engine = engineWithSevenHits();
        SearchResult page1 = engine.search("term", 3, null);

        assertThrows(SearchException.CursorMismatch.class,
                () -> engine.search("other", 3, page1.nextCursor()),
                "different query with old cursor");
        assertThrows(SearchException.CursorMismatch.class,
                () -> engine.search("term", 5, page1.nextCursor()),
                "different pageSize with old cursor");
    }

    @TestCase
    static void malformedCursorRejected() {
        SearchEngine engine = engineWithSevenHits();
        assertThrows(SearchException.InvalidCursor.class,
                () -> engine.search("term", 3, "not-a-cursor!!!"),
                "garbage cursor");
        // 合法 base64 但内容不是游标 JSON
        String fake = java.util.Base64.getUrlEncoder().withoutPadding()
                .encodeToString("{\"foo\":1}".getBytes(java.nio.charset.StandardCharsets.UTF_8));
        assertThrows(SearchException.InvalidCursor.class,
                () -> engine.search("term", 3, fake),
                "json without cursor fields");
    }

    @TestCase
    static void invalidParametersRejected() {
        SearchEngine engine = engineWithSevenHits();
        assertThrows(SearchException.BadRequest.class,
                () -> engine.search("", 3, null), "blank query");
        assertThrows(SearchException.BadRequest.class,
                () -> engine.search("term", 0, null), "pageSize 0");
        assertThrows(SearchException.BadRequest.class,
                () -> engine.search("term", 101, null), "pageSize over max");
    }

    @TestCase
    static void cursorRoundTrip() {
        SearchCursor cursor = new SearchCursor(3, "hello world", 10, 1.25, "doc-7", 20);
        SearchCursor decoded = SearchCursor.decode(cursor.encode());
        assertEquals(cursor, decoded, "cursor encode/decode round trip");
    }
}
