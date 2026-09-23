package com.bm25stable;

import java.util.List;

/**
 * 一页检索结果。
 *
 * @param snapshotVersion 本页使用的索引快照版本
 * @param totalHits       该快照下查询的总命中数（同一快照内恒定）
 * @param offset          本页起始偏移（含历史页累计）
 * @param pageSize        每页大小
 * @param hits            本页命中
 * @param nextCursor      下一页游标；hasMore 为 false 时为 null
 * @param hasMore         是否还有后续页
 * @param snapshot        本页使用的快照（供 HTTP 层取文档原文，不参与 JSON 序列化）
 */
public record SearchResult(int snapshotVersion, int totalHits, int offset, int pageSize,
                           List<SearchHit> hits, String nextCursor, boolean hasMore,
                           IndexSnapshot snapshot) {
}
