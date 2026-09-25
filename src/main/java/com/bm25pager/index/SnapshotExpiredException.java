package com.bm25pager.index;

import java.util.LinkedHashMap;

/**
 * 游标引用的索引快照已不存在（被保留策略驱逐）时抛出。
 * HTTP 层映射为 410 Gone。
 */
public class SnapshotExpiredException extends RuntimeException {

    private static final long serialVersionUID = 1L;

    private final long requestedVersion;
    private final long currentVersion;

    public SnapshotExpiredException(long requestedVersion, long currentVersion) {
        super("snapshot version " + requestedVersion
                + " has expired; current version is " + currentVersion);
        this.requestedVersion = requestedVersion;
        this.currentVersion = currentVersion;
    }

    public long requestedVersion() {
        return requestedVersion;
    }

    public long currentVersion() {
        return currentVersion;
    }
}
