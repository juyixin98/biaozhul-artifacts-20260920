package com.example.paginate.snapshot;

import com.example.paginate.model.Item;
import com.example.paginate.web.ApiException;

import java.security.SecureRandom;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicLong;
import java.util.function.Supplier;

/**
 * 快照注册表：创建、查找、过期判断与惰性清理。
 *
 * - 快照过期 -> 410 snapshot_expired（明确报错，不是静默回到最新数据）
 * - 快照不存在（从未有过/已清理/伪造ID）-> 404 snapshot_not_found
 */
public final class SnapshotManager {

    private static final int SWEEP_THRESHOLD = 256;
    private static final SecureRandom RANDOM = new SecureRandom();

    private final Map<String, Snapshot> snapshots = new ConcurrentHashMap<>();
    private final AtomicLong versionSeq = new AtomicLong(0);
    private final Duration ttl;
    private final Clock clock;

    public SnapshotManager(Duration ttl) {
        this(ttl, Clock.systemUTC());
    }

    public SnapshotManager(Duration ttl, Clock clock) {
        this.ttl = ttl;
        this.clock = clock;
    }

    public Duration ttl() {
        return ttl;
    }

    public int retainedCount() {
        return snapshots.size();
    }

    public Snapshot create(Supplier<List<Item>> dataCopy) {
        String id = newId();
        long version = versionSeq.incrementAndGet();
        Instant now = clock.instant();
        Snapshot snapshot = new Snapshot(id, version, now, List.copyOf(dataCopy.get()));
        snapshots.put(id, snapshot);
        maybeSweep(now);
        return snapshot;
    }

    /**
     * 按 ID 取快照并校验过期。
     *
     * @throws ApiException 410 已过期；404 不存在
     */
    public Snapshot require(String snapshotId) {
        Snapshot snapshot = snapshots.get(snapshotId);
        Instant now = clock.instant();
        if (snapshot == null) {
            // 可能确实过期后被惰性清理掉了：无法区分，统一按 not found 返回
            throw ApiException.notFound("snapshot_not_found",
                    "快照不存在或已被清理: " + snapshotId + "，请重新发起第一页查询");
        }
        if (isExpired(snapshot, now)) {
            throw ApiException.gone("snapshot_expired",
                    "快照 " + snapshotId + " 已过期（TTL=" + ttl.getSeconds()
                            + " 秒，创建于 " + snapshot.createdAt() + "），请重新发起第一页查询");
        }
        return snapshot;
    }

    public boolean isExpired(Snapshot snapshot, Instant now) {
        return !snapshot.createdAt().plus(ttl).isAfter(now);
    }

    private void maybeSweep(Instant now) {
        if (snapshots.size() < SWEEP_THRESHOLD) {
            return;
        }
        // 过期两倍 TTL 后才真正移除：在此之前访问仍能给出 410 而非 404
        Duration tombstoneWindow = ttl.multipliedBy(2);
        snapshots.entrySet().removeIf(
                e -> e.getValue().createdAt().plus(tombstoneWindow).isBefore(now));
    }

    private static String newId() {
        byte[] b = new byte[12];
        RANDOM.nextBytes(b);
        StringBuilder sb = new StringBuilder(24);
        for (byte x : b) {
            sb.append(Character.forDigit((x >> 4) & 0xF, 16));
            sb.append(Character.forDigit(x & 0xF, 16));
        }
        return sb.toString();
    }
}
