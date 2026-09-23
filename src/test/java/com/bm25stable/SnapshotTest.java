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

/** 快照隔离测试：更新不影响旧游标页面；快照淘汰后报 SNAPSHOT_EXPIRED。 */
public final class SnapshotTest {

    @TestCase
    static void updatesDoNotAffectInFlightPagination() {
        SearchEngine engine = new SearchEngine();
        for (int i = 1; i <= 5; i++) {
            engine.upsert("doc-" + i, "apple pie " + i);
        }
        SearchResult page1 = engine.search("apple", 2, null);
        int snapshotVersion = page1.snapshotVersion();
        assertEquals(2, page1.hits().size(), "page1 size");
        assertTrue(page1.hasMore(), "page1 hasMore");

        // 语料变更：新增命中文档、删除已有文档、修改已有文档
        engine.upsert("doc-6", "apple pie 6 extra");
        engine.delete("doc-1");
        engine.upsert("doc-2", "apple apple apple apple");
        assertTrue(engine.currentVersion() > snapshotVersion, "version advanced");

        // 旧游标继续翻页：仍走旧快照，结果与变更前一致
        List<String> collected = new ArrayList<>();
        page1.hits().forEach(h -> collected.add(h.docId()));
        String cursor = page1.nextCursor();
        while (cursor != null) {
            SearchResult page = engine.search("apple", 2, cursor);
            assertEquals(snapshotVersion, page.snapshotVersion(), "page uses old snapshot");
            assertEquals(5, page.totalHits(), "totalHits frozen at snapshot");
            page.hits().forEach(h -> collected.add(h.docId()));
            cursor = page.hasMore() ? page.nextCursor() : null;
        }
        assertEquals(5, collected.size(), "all 5 original docs collected");
        assertEquals(new LinkedHashSet<>(collected).size(), 5, "no duplicates");
        assertFalse(collected.contains("doc-6"), "new doc not visible to old cursor");
        assertTrue(collected.contains("doc-1"), "deleted doc still visible to old cursor");

        // 新查询（无游标）使用新快照，能看到变更
        SearchResult fresh = engine.search("apple", 100, null);
        assertTrue(fresh.snapshotVersion() > snapshotVersion, "fresh search uses newer snapshot");
        assertEquals(5, fresh.totalHits(), "5 docs after +1 -1");
        assertTrue(fresh.hits().stream().anyMatch(h -> h.docId().equals("doc-6")),
                "new doc visible in fresh search");
        assertFalse(fresh.hits().stream().anyMatch(h -> h.docId().equals("doc-1")),
                "deleted doc gone in fresh search");
    }

    @TestCase
    static void expiredSnapshotYields410Semantics() {
        SearchEngine engine = new SearchEngine();
        engine.upsert("doc-1", "apple one");
        engine.upsert("doc-2", "apple two");
        engine.upsert("doc-3", "apple three");
        SearchResult page1 = engine.search("apple", 2, null);
        int oldVersion = page1.snapshotVersion();
        String cursor = page1.nextCursor();

        // 触发足够多次变更，把旧快照挤出保留窗口（保留 8 个）
        for (int i = 0; i < SearchEngine.MAX_RETAINED_SNAPSHOTS; i++) {
            engine.upsert("churn-" + i, "churn " + i);
        }
        assertFalse(engine.retainedVersions().contains(oldVersion), "old snapshot evicted");

        SearchException.SnapshotExpired error = assertThrows(SearchException.SnapshotExpired.class,
                () -> engine.search("apple", 2, cursor),
                "expired cursor must fail");
        assertEquals(410, error.status(), "http status");
        assertEquals("SNAPSHOT_EXPIRED", error.code(), "error code");
    }

    @TestCase
    static void retainedSnapshotsAreBounded() {
        SearchEngine engine = new SearchEngine();
        for (int i = 0; i < 20; i++) {
            engine.upsert("doc-" + i, "text " + i);
        }
        assertEquals(SearchEngine.MAX_RETAINED_SNAPSHOTS, engine.retainedVersions().size(),
                "retained snapshot count capped");
        assertEquals(20, engine.currentVersion(), "version keeps increasing");
        assertTrue(engine.retainedVersions().contains(20), "latest retained");
        assertFalse(engine.retainedVersions().contains(12), "older evicted");
    }

    @TestCase
    static void noOpWritesDoNotCreateSnapshots() {
        SearchEngine engine = new SearchEngine();
        engine.upsert("doc-1", "apple");
        int version = engine.currentVersion();
        engine.upsert("doc-1", "apple");          // 内容相同
        engine.delete("doc-nonexistent");          // 删除不存在的文档
        assertEquals(version, engine.currentVersion(), "no new snapshot for no-op writes");
    }

    @TestCase
    static void deleteAffectsOnlyNewSearches() {
        SearchEngine engine = new SearchEngine();
        engine.upsert("doc-1", "apple");
        engine.upsert("doc-2", "apple");
        SearchResult before = engine.search("apple", 10, null);
        assertEquals(2, before.totalHits(), "two hits before delete");

        engine.delete("doc-1");
        SearchResult after = engine.search("apple", 10, null);
        assertEquals(1, after.totalHits(), "one hit after delete");
        assertEquals("doc-2", after.hits().get(0).docId(), "remaining doc");
    }
}
