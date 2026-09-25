package com.bm25pager.search;

import com.bm25pager.index.IndexManager;
import com.bm25pager.index.IndexSnapshot;
import com.bm25pager.text.Tokenizer;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 检索服务：把 {@link IndexSnapshot#rank} 产出的确定性排名按游标切片分页。
 *
 * 分页是“键集分页（keyset pagination）”而不是 offset 分页：
 * 游标携带上一页最后一条记录的 (score, docId) 与它绑定的快照版本，
 * 每一页都在同一个不可变快照上重新计算排名并从边界后续读起。
 * 因此即使索引已更新，旧游标看到的仍是旧快照的结果，无漏读、无重复。
 */
public final class SearchService {

    /** 返回给客户端的正文片段最大长度。 */
    static final int SNIPPET_LENGTH = 120;

    /** 页大小上限。 */
    static final int MAX_PAGE_SIZE = 100;

    /** 页大小缺省值。 */
    static final int DEFAULT_PAGE_SIZE = 10;

    private final IndexManager indexManager;

    public SearchService(IndexManager indexManager) {
        this.indexManager = indexManager;
    }

    /**
     * 第一页查询（无游标）。
     *
     * @param queryText 原始查询串，按固定规则分词
     * @param pageSize  页大小；null 用缺省值；范围裁剪到 [1, 100]
     */
    public SearchResult search(String queryText, Integer pageSize) {
        List<String> terms = Tokenizer.tokenize(queryText == null ? "" : queryText);
        int size = normalizePageSize(pageSize);

        IndexSnapshot snapshot = indexManager.currentSnapshot();
        Cursor cursor = Cursor.firstPage(snapshot.version(), terms, size);
        return execute(snapshot, cursor, terms);
    }

    /**
     * 后续页查询（带游标）。
     * 页大小、查询词项、快照版本全部以游标为准 —— 调用方传入的 query/pageSize 被忽略，
     * 避免同一次翻页过程中参数漂移。
     *
     * @throws InvalidCursorException       游标无法解码
     * @throws com.bm25pager.index.SnapshotExpiredException 游标绑定的快照已被驱逐
     */
    public SearchResult searchAfter(String cursorToken) {
        Cursor cursor = Cursor.decode(cursorToken);
        IndexSnapshot snapshot = indexManager.requireSnapshot(cursor.version);
        return execute(snapshot, cursor, cursor.terms);
    }

    private SearchResult execute(IndexSnapshot snapshot, Cursor cursor, List<String> terms) {
        List<Hit> ranked = snapshot.rank(terms);

        int fromIndex = 0;
        if (cursor.page > 0) {
            fromIndex = findBoundary(ranked, cursor);
        }

        int toIndex = Math.min(fromIndex + cursor.pageSize, ranked.size());
        List<Hit> page = (fromIndex >= ranked.size())
                ? List.of()
                : new ArrayList<>(ranked.subList(fromIndex, toIndex));

        Cursor nextCursor = null;
        boolean hasMore = toIndex < ranked.size();
        if (hasMore) {
            Hit last = page.get(page.size() - 1);
            nextCursor = new Cursor(
                    snapshot.version(),
                    terms,
                    cursor.pageSize,
                    cursor.page + 1,
                    last.score(),
                    last.docId());
        }

        return new SearchResult(
                snapshot.version(),
                cursor.page,
                cursor.pageSize,
                ranked.size(),
                page,
                nextCursor == null ? null : nextCursor.encode(),
                hasMore);
    }

    /**
     * 在完整排名中定位“上一页最后一条记录”的下一条位置。
     * 用游标保存的精确 double 分数与 docId 匹配，而非页码/偏移量。
     */
    private int findBoundary(List<Hit> ranked, Cursor cursor) {
        int found = -1;
        for (int i = 0; i < ranked.size(); i++) {
            Hit h = ranked.get(i);
            if (Double.compare(h.score(), cursor.lastScore) == 0
                    && h.docId().equals(cursor.lastId)) {
                found = i;
                break;
            }
        }
        if (found < 0) {
            // 快照本身不可变，理论上不可能发生；出现即说明数据损坏
            throw new IllegalStateException(
                    "cursor boundary (score=" + cursor.lastScore + ", docId=" + cursor.lastId
                            + ") not found in snapshot v" + cursor.version);
        }
        return found + 1;
    }

    private static int normalizePageSize(Integer requested) {
        if (requested == null) {
            return DEFAULT_PAGE_SIZE;
        }
        return Math.max(1, Math.min(MAX_PAGE_SIZE, requested));
    }

    /**
     * 把命中列表渲染为 JSON 友好的结构（Map/List/基本类型），正文附带片段。
     */
    public static List<Map<String, Object>> renderHits(List<Hit> hits) {
        List<Map<String, Object>> out = new ArrayList<>(hits.size());
        for (Hit h : hits) {
            Map<String, Object> item = new LinkedHashMap<>();
            item.put("docId", h.docId());
            item.put("score", h.score());
            item.put("snippet", snippet(h.document().content()));
            item.put("metadata", h.document().metadata());
            out.add(item);
        }
        return out;
    }

    static String snippet(String content) {
        String s = content.replaceAll("\\s+", " ").trim();
        if (s.length() <= SNIPPET_LENGTH) {
            return s;
        }
        return s.substring(0, SNIPPET_LENGTH) + "…";
    }
}
