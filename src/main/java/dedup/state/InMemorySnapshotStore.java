package dedup.state;

import dedup.json.Json;

/** 内存快照存储（库级测试用：模拟“重启”= 用同一份快照重建算子）。 */
public final class InMemorySnapshotStore implements SnapshotStore {

    private SnapshotData latest;

    @Override
    public synchronized void save(SnapshotData snapshot) {
        // 重新序列化/解析一次，确保快照确实是自包含、可外部化的
        latest = SnapshotData.fromJson(Json.parse(Json.write(snapshot.toJson())));
    }

    @Override
    public synchronized SnapshotData load() {
        if (latest == null) return null;
        return SnapshotData.fromJson(Json.parse(Json.write(latest.toJson())));
    }

    @Override
    public synchronized void clear() {
        latest = null;
    }

    @Override
    public synchronized boolean exists() {
        return latest != null;
    }
}
