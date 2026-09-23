package com.example.paginate.page;

import com.example.paginate.cursor.CursorPayload;
import com.example.paginate.cursor.CursorService;
import com.example.paginate.model.Item;
import com.example.paginate.query.PageQuery;
import com.example.paginate.snapshot.Snapshot;
import com.example.paginate.snapshot.SnapshotManager;
import com.example.paginate.store.ItemStore;
import com.example.paginate.web.ApiException;

import java.util.ArrayList;
import java.util.List;

/**
 * 版本化结果集分页核心。
 *
 * 第一页：为“当前查询”新建快照，返回首页与下一页游标。
 * 后续页：校验游标（MAC + 查询指纹）-> 取绑定的快照（过期明确报错）
 *         -> 在同一份不可变数据上按“最后排序元组”严格向后推进。
 *
 * 因为页与页之间共享同一份快照拷贝且排序是 (排序键,id) 全序，
 * 翻页期间对活数据的插入/删除/改排序键都不会造成重页或漏页。
 */
public final class PaginationService {

    private final ItemStore store;
    private final SnapshotManager snapshots;
    private final CursorService cursors;

    public PaginationService(ItemStore store, SnapshotManager snapshots, CursorService cursors) {
        this.store = store;
        this.snapshots = snapshots;
        this.cursors = cursors;
    }

    /** 第一页：无游标，建立新快照。 */
    public PageResult firstPage(PageQuery query) {
        Snapshot snapshot = snapshots.create(store::snapshotItems);
        return buildPage(snapshot, query, null, null);
    }

    /** 后续页：携带游标。 */
    public PageResult nextPage(PageQuery query, String cursorToken) {
        CursorPayload payload = cursors.decode(cursorToken, query.queryHash());
        // 快照过期/不存在在这里得到明确错误（410/404），不会静默回退到最新数据
        Snapshot snapshot = snapshots.require(payload.snapshotId());
        return buildPage(snapshot, query, payload.lastSortValue(), payload.lastId());
    }

    private PageResult buildPage(Snapshot snapshot, PageQuery query,
                                 String anchorSortValue, Long anchorId) {
        List<Item> ordered = new ArrayList<>(snapshot.items().size());
        for (Item item : snapshot.items()) {
            if (!query.matches(item)) {
                continue;
            }
            if (anchorSortValue != null
                    && !query.strictlyAfter(item, anchorSortValue, anchorId)) {
                continue; // 锚点之前（含锚点本身）的记录全部跳过 -> 不重不漏
            }
            ordered.add(item);
        }
        ordered.sort(query.comparator());

        int end = Math.min(query.pageSize(), ordered.size());
        List<Item> pageItems = new ArrayList<>(ordered.subList(0, end));

        String nextCursor = null;
        // 仅当确实取满一页且后面还有记录时才发游标，避免多余的空翻页
        if (pageItems.size() == query.pageSize() && ordered.size() > query.pageSize()) {
            Item last = pageItems.get(pageItems.size() - 1);
            nextCursor = cursors.encode(new CursorPayload(
                    query.queryHash(),
                    snapshot.id(),
                    query.sortValueOf(last),
                    last.id()));
        }
        return new PageResult(pageItems, nextCursor, nextCursor != null,
                snapshot.id(), snapshot.version(), query.pageSize());
    }
}
