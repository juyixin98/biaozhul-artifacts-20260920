package com.example.paginate.snapshot;

import com.example.paginate.model.Item;

import java.time.Instant;
import java.util.List;

/**
 * 查询快照：某次查询在某个时刻对结果集的不可变拷贝。
 * 游标通过 snapshotId 绑定快照；TTL 过期后使用该游标会得到明确错误。
 */
public record Snapshot(
        String id,
        long version,
        Instant createdAt,
        List<Item> items
) {
    /** 版本号自增（每次创建快照 +1），也可帮助排查快照先后。 */
}
