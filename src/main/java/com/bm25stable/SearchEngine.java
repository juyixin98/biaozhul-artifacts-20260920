package com.bm25stable;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

/**
 * 检索引擎：维护可变语料 + 不可变快照序列，提供稳定分页检索。
 *
 * <p>核心语义：
 * <ul>
 *   <li>每次语料变更（upsert/delete 且内容确有变化）生成一个新版本快照；</li>
 *   <li>第一页请求（无游标）总是使用最新快照，游标记录该版本；</li>
 *   <li>后续页凭游标复用同一快照，期间语料更新不影响已开始的分页；</li>
 *   <li>最多保留 {@value #MAX_RETAINED_SNAPSHOTS} 个快照，游标指向已淘汰版本时报
 *       {@link SearchException.SnapshotExpired}（HTTP 410）；</li>
 *   <li>排序固定为 (score 降序, docId 字典序升序)，保证同分有确定次序、
 *       连续分页不漏不重。</li>
 * </ul>
 */
public final class SearchEngine {

    /** 保留的历史快照个数（含当前最新）。 */
    public static final int MAX_RETAINED_SNAPSHOTS = 8;
    public static final int MAX_PAGE_SIZE = 100;

    /** 当前语料（TreeMap 保证遍历有序）。 */
    private final Map<String, String> docs = new TreeMap<>();
    /** version -> snapshot，按版本升序。 */
    private final Map<Integer, IndexSnapshot> snapshots = new LinkedHashMap<>();
    private int currentVersion = 0;

    // ------------------------------------------------------------------
    // 语料写入
    // ------------------------------------------------------------------

    /** 新增或覆盖一篇文档；内容未变化时不产生新版本。返回当前版本号。 */
    public synchronized int upsert(String id, String text) {
        if (id == null || id.isBlank()) {
            throw new SearchException.BadRequest("document id must not be blank");
        }
        String safeText = text == null ? "" : text;
        if (safeText.equals(docs.get(id))) {
            return currentVersion;
        }
        docs.put(id, safeText);
        return rebuildSnapshot();
    }

    public synchronized int upsertAll(Map<String, String> batch) {
        boolean changed = false;
        for (Map.Entry<String, String> e : batch.entrySet()) {
            String safeText = e.getValue() == null ? "" : e.getValue();
            if (!safeText.equals(docs.get(e.getKey()))) {
                docs.put(e.getKey(), safeText);
                changed = true;
            }
        }
        return changed ? rebuildSnapshot() : currentVersion;
    }

    /** 删除文档；不存在时不产生新版本。返回是否确有删除。 */
    public synchronized boolean delete(String id) {
        if (docs.remove(id) != null) {
            rebuildSnapshot();
            return true;
        }
        return false;
    }

    private int rebuildSnapshot() {
        currentVersion++;
        IndexSnapshot snapshot = IndexSnapshot.build(currentVersion, docs);
        snapshots.put(currentVersion, snapshot);
        while (snapshots.size() > MAX_RETAINED_SNAPSHOTS) {
            snapshots.remove(snapshots.keySet().iterator().next());
        }
        return currentVersion;
    }

    // ------------------------------------------------------------------
    // 状态查询
    // ------------------------------------------------------------------

    public synchronized int currentVersion() {
        return currentVersion;
    }

    public synchronized int docCount() {
        return docs.size();
    }

    public synchronized List<Integer> retainedVersions() {
        return List.copyOf(snapshots.keySet());
    }

    public synchronized IndexSnapshot snapshotAt(int version) {
        return snapshots.get(version);
    }

    /** 当前最新快照；语料为空且从未变更时按需构建 version 1。 */
    public synchronized IndexSnapshot latestSnapshot() {
        if (currentVersion == 0) {
            rebuildSnapshot();
        }
        return snapshots.get(currentVersion);
    }

    // ------------------------------------------------------------------
    // 检索
    // ------------------------------------------------------------------

    /**
     * 稳定分页检索。
     *
     * @param query    查询文本（必填，分词规则与索引一致）
     * @param pageSize 每页大小，1..{@value #MAX_PAGE_SIZE}
     * @param cursor   上一页返回的游标；null 表示第一页
     */
    public synchronized SearchResult search(String query, int pageSize, String cursor) {
        if (query == null || query.isBlank()) {
            throw new SearchException.BadRequest("query must not be blank");
        }
        if (pageSize < 1 || pageSize > MAX_PAGE_SIZE) {
            throw new SearchException.BadRequest(
                    "pageSize must be between 1 and " + MAX_PAGE_SIZE + ", got " + pageSize);
        }
        String canonicalQuery = canonicalQuery(query);

        final SearchCursor cur;
        final IndexSnapshot snapshot;
        if (cursor == null || cursor.isBlank()) {
            cur = null;
            snapshot = latestSnapshot();
        } else {
            cur = SearchCursor.decode(cursor);
            if (!cur.query().equals(canonicalQuery)) {
                throw new SearchException.CursorMismatch(
                        "cursor was issued for query '" + cur.query() + "' but request query is '" + canonicalQuery + "'");
            }
            if (cur.pageSize() != pageSize) {
                throw new SearchException.CursorMismatch(
                        "cursor was issued with pageSize " + cur.pageSize() + " but request pageSize is " + pageSize);
            }
            snapshot = snapshots.get(cur.version());
            if (snapshot == null) {
                throw new SearchException.SnapshotExpired(
                        "snapshot version " + cur.version() + " has been evicted (retained: "
                                + retainedVersions() + "); restart pagination from the first page");
            }
        }

        List<SearchHit> all = scoreAll(snapshot, canonicalQuery);
        int start = 0;
        if (cur != null) {
            start = positionAfter(all, cur.lastScore(), cur.lastDocId());
        }
        int end = Math.min(start + pageSize, all.size());
        List<SearchHit> page = List.copyOf(all.subList(start, end));

        boolean hasMore = end < all.size();
        String nextCursor = null;
        if (hasMore && !page.isEmpty()) {
            SearchHit last = page.get(page.size() - 1);
            nextCursor = new SearchCursor(snapshot.version(), canonicalQuery, pageSize,
                    last.score(), last.docId(), end).encode();
        }
        return new SearchResult(snapshot.version(), all.size(), start, pageSize,
                page, nextCursor, hasMore, snapshot);
    }

    /** 规范化查询：分词、去重（保留首次出现顺序），用空格连接。 */
    static String canonicalQuery(String query) {
        return String.join(" ", new LinkedHashSet<>(Tokenizer.tokenize(query)));
    }

    /** 全量打分并排序：score 降序，同分按 docId 字典序升序。 */
    private List<SearchHit> scoreAll(IndexSnapshot snapshot, String canonicalQuery) {
        List<SearchHit> hits = new ArrayList<>();
        if (canonicalQuery.isEmpty() || snapshot.docCount() == 0) {
            return hits;
        }
        Map<String, Double> scores = new LinkedHashMap<>();
        for (String term : canonicalQuery.split(" ")) {
            Map<String, Integer> postings = snapshot.postings(term);
            if (postings == null) {
                continue;
            }
            int df = postings.size();
            for (Map.Entry<String, Integer> posting : postings.entrySet()) {
                double contribution = BM25.termScore(
                        posting.getValue(),
                        snapshot.docLength(posting.getKey()),
                        snapshot.avgDocLength(),
                        snapshot.docCount(),
                        df);
                scores.merge(posting.getKey(), contribution, Double::sum);
            }
        }
        for (Map.Entry<String, Double> e : scores.entrySet()) {
            hits.add(new SearchHit(e.getKey(), e.getValue()));
        }
        hits.sort(Comparator.comparingDouble(SearchHit::score).reversed()
                .thenComparing(SearchHit::docId));
        return hits;
    }

    /** 键集定位：返回严格位于 (lastScore, lastDocId) 之后的第一个下标。 */
    private static int positionAfter(List<SearchHit> sorted, double lastScore, String lastDocId) {
        int i = 0;
        while (i < sorted.size()) {
            SearchHit h = sorted.get(i);
            boolean before = h.score() > lastScore
                    || (h.score() == lastScore && h.docId().compareTo(lastDocId) <= 0);
            if (!before) {
                break;
            }
            i++;
        }
        return i;
    }
}
