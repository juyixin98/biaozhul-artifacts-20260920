package com.example.paginate.cursor;

/**
 * 游标明文负载（不透明字符串的内部结构）：
 *
 *   v=1
 *   q  查询指纹（SHA-256 截断），绑定 sort/category/q/pageSize
 *   s  快照 ID，绑定快照与版本
 *   k  上一页最后一条记录的排序键值（name 原文或 score 十进制）
 *   i  上一页最后一条记录的唯一 ID（最终决胜键）
 *
 * 序列化形式为 payloadBase64url "." macBase64url；客户端只能原样回传。
 */
public record CursorPayload(
        String queryHash,
        String snapshotId,
        String lastSortValue,
        long lastId
) {
    public static final int VERSION = 1;
}
