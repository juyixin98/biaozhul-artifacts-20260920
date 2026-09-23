package com.example.paginate.page;

import com.example.paginate.model.Item;

import java.util.List;

/**
 * 一页结果。
 *
 * @param items      本页记录
 * @param nextCursor 下一页游标；没有更多数据时为 null
 * @param hasMore    是否还有下一页
 * @param snapshotId 本页使用的快照 ID
 * @param snapshotVersion 快照版本号
 * @param pageSize   生效的页大小
 */
public record PageResult(
        List<Item> items,
        String nextCursor,
        boolean hasMore,
        String snapshotId,
        long snapshotVersion,
        int pageSize
) {
}
