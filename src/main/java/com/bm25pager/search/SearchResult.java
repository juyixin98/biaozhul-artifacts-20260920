package com.bm25pager.search;

import java.util.List;

/** 一次分页查询的完整结果。 */
public final class SearchResult {

    private final long version;
    private final int page;
    private final int pageSize;
    private final int totalHits;
    private final List<Hit> hits;
    private final String nextCursor;
    private final boolean hasMore;

    public SearchResult(long version,
                        int page,
                        int pageSize,
                        int totalHits,
                        List<Hit> hits,
                        String nextCursor,
                        boolean hasMore) {
        this.version = version;
        this.page = page;
        this.pageSize = pageSize;
        this.totalHits = totalHits;
        this.hits = List.copyOf(hits);
        this.nextCursor = nextCursor;
        this.hasMore = hasMore;
    }

    public long version() {
        return version;
    }

    public int page() {
        return page;
    }

    public int pageSize() {
        return pageSize;
    }

    public int totalHits() {
        return totalHits;
    }

    public List<Hit> hits() {
        return hits;
    }

    /** 没有下一页时为 null。 */
    public String nextCursor() {
        return nextCursor;
    }

    public boolean hasMore() {
        return hasMore;
    }
}
