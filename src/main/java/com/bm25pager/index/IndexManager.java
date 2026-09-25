package com.bm25pager.index;

import com.bm25pager.model.Document;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.NavigableMap;
import java.util.TreeMap;

/**
 * 索引管理器：维护“工作区”文档与已发布的不可变快照。
 *
 * 工作方式：
 * - upsert / delete 只改工作区，对外检索不可见；
 * - commit() 把工作区固化成一个新版本的 {@link IndexSnapshot}（version 从 1 递增）。
 *   首次搜索会自动得到当前版本的游标；翻页游标始终携带它绑定的 version；
 * - 快照一旦发布不可修改。旧游标继续读取旧快照，因此“更新不影响旧游标页面”；
 * - 快照按版本保留最近 {@code retainSnapshots} 个（至少保留 1 个）。
 *   被驱逐的旧版本若仍被游标引用，读取时抛出 {@link SnapshotExpiredException}，
 *   由 HTTP 层返回“快照过期”错误，调用方需用第一页重新开始。
 *
 * 所有公共方法均 synchronized，保证并发更新/检索安全。
 */
public final class IndexManager {

    private final int retainSnapshots;

    /** 当前工作区（尚未发布的修改也在这里，与最新快照共享存储，commit 才固化）。 */
    private final Map<String, Document> workingDocs = new LinkedHashMap<>();

    /** 已发布快照，按版本升序；旧版本在头部，驱逐时从头移除。 */
    private final NavigableMap<Long, IndexSnapshot> snapshots = new TreeMap<>();

    private long nextVersion = 1;
    private long currentVersion = 0;
    private boolean dirty = false;

    public IndexManager(int retainSnapshots) {
        if (retainSnapshots < 1) {
            throw new IllegalArgumentException("retainSnapshots must be >= 1");
        }
        this.retainSnapshots = retainSnapshots;
    }

    /** 初始批量装载（用于启动时加载合成语料），之后自动发布 v1。 */
    public synchronized void loadInitial(List<Document> docs) {
        if (currentVersion != 0) {
            throw new IllegalStateException("initial load only allowed before any commit");
        }
        for (Document d : docs) {
            workingDocs.put(d.docId(), d);
        }
        commit();
    }

    public synchronized void upsert(Document doc) {
        workingDocs.put(doc.docId(), doc);
        dirty = true;
    }

    /** 删除不存在的文档返回 false；不立即产生新版本。 */
    public synchronized boolean delete(String docId) {
        boolean removed = workingDocs.remove(docId) != null;
        if (removed) {
            dirty = true;
        }
        return removed;
    }

    /** 若工作区有修改则发布新快照；无修改时返回当前版本。 */
    public synchronized long commit() {
        if (!dirty && currentVersion != 0) {
            return currentVersion;
        }
        long version = nextVersion++;
        IndexSnapshot snapshot = IndexSnapshot.build(version, System.currentTimeMillis(), workingDocs);
        snapshots.put(version, snapshot);
        currentVersion = version;
        dirty = false;
        evict();
        return version;
    }

    private void evict() {
        while (snapshots.size() > retainSnapshots) {
            Long oldest = snapshots.firstKey();
            snapshots.remove(oldest);
        }
    }

    /**
     * 主动压缩历史：只保留最近 keep 个快照（最少 1 个）。
     * 用于演示“快照过期”：压掉游标绑定的旧版本后，继续翻页会收到过期错误。
     */
    public synchronized int compact(int keep) {
        int target = Math.max(1, keep);
        int removed = 0;
        while (snapshots.size() > target) {
            snapshots.remove(snapshots.firstKey());
            removed++;
        }
        return removed;
    }

    public synchronized IndexSnapshot currentSnapshot() {
        return snapshots.get(currentVersion);
    }

    public synchronized long currentVersion() {
        return currentVersion;
    }

    /**
     * 按版本获取快照；不存在则说明已被驱逐（过期）。
     */
    public synchronized IndexSnapshot requireSnapshot(long version) {
        IndexSnapshot snapshot = snapshots.get(version);
        if (snapshot == null) {
            throw new SnapshotExpiredException(version, currentVersion);
        }
        return snapshot;
    }

    public synchronized List<Long> retainedVersions() {
        return new ArrayList<>(snapshots.navigableKeySet());
    }
}
