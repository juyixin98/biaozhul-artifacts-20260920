package dedup;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.Iterator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Streaming event-ID deduplication with watermark-bounded retention.
 *
 * Semantics:
 * - Each event has an id and an event time (epoch ms). Events are sharded into a
 *   fixed number of partitions by hash(eventId).
 * - Per partition a watermark is maintained: watermark = maxEventTimeSeen - allowedLatenessMs
 *   (it can also be advanced explicitly, e.g. by an external source of watermarks).
 * - An event id is remembered while eventTime + retentionMs > watermark. A repeated
 *   id inside that window is a DUPLICATE.
 * - Once the watermark reaches eventTime + retentionMs the entry is evicted and the
 *   same id is treated as a NEW event again ("retention expired").
 * - Every mutating request carries a routing version; a version different from the
 *   service's current one is rejected with {@link StaleRoutingVersionException}.
 * - Partition state can be exported and imported so dedup state follows the keys
 *   when a partition is migrated to another node.
 */
public class DedupService {

    public enum Status { NEW, DUPLICATE }

    public record SubmitResult(Status status, int partition, long watermark) {}

    /** Serializable snapshot of one partition's dedup state. */
    public record PartitionSnapshot(
            int partition,
            long routingVersion,
            long watermark,
            long maxEventTime,
            Map<String, Long> entries) {}

    private final int numPartitions;
    private final long allowedLatenessMs;
    private final long retentionMs;

    private volatile long routingVersion = 1;

    private static final class PartitionState {
        long watermark = Long.MIN_VALUE;
        long maxEventTime = Long.MIN_VALUE;
        final Map<String, Long> entries = new HashMap<>(); // eventId -> eventTime
    }

    private final PartitionState[] partitions;

    public DedupService(int numPartitions, long allowedLatenessMs, long retentionMs) {
        if (numPartitions <= 0) throw new IllegalArgumentException("numPartitions must be > 0");
        if (allowedLatenessMs < 0) throw new IllegalArgumentException("allowedLatenessMs must be >= 0");
        if (retentionMs < 0) throw new IllegalArgumentException("retentionMs must be >= 0");
        this.numPartitions = numPartitions;
        this.allowedLatenessMs = allowedLatenessMs;
        this.retentionMs = retentionMs;
        this.partitions = new PartitionState[numPartitions];
        for (int i = 0; i < numPartitions; i++) partitions[i] = new PartitionState();
    }

    public long routingVersion() { return routingVersion; }

    public int partitionFor(String eventId) {
        return Math.floorMod(eventId.hashCode(), numPartitions);
    }

    private void checkVersion(long version) {
        if (version != routingVersion) throw new StaleRoutingVersionException(routingVersion, version);
    }

    /** Submit an event; returns NEW or DUPLICATE. */
    public SubmitResult submit(String eventId, long eventTime, long version) {
        checkVersion(version);
        int p = partitionFor(eventId);
        PartitionState st = partitions[p];
        synchronized (st) {
            st.maxEventTime = Math.max(st.maxEventTime, eventTime);
            advanceLocked(st, st.maxEventTime - allowedLatenessMs);
            Status status = st.entries.containsKey(eventId) ? Status.DUPLICATE : Status.NEW;
            // Remember (or refresh with the latest observed event time for this id).
            st.entries.merge(eventId, eventTime, Math::max);
            return new SubmitResult(status, p, st.watermark);
        }
    }

    /** Explicitly advance a partition's watermark; returns how many entries were evicted. */
    public int advanceWatermark(int partition, long watermark, long version) {
        checkVersion(version);
        PartitionState st = partitions[partition];
        synchronized (st) {
            return advanceLocked(st, watermark);
        }
    }

    private int advanceLocked(PartitionState st, long newWatermark) {
        if (newWatermark <= st.watermark) return 0;
        st.watermark = newWatermark;
        int evicted = 0;
        Iterator<Map.Entry<String, Long>> it = st.entries.entrySet().iterator();
        while (it.hasNext()) {
            Map.Entry<String, Long> e = it.next();
            // Entry expires when watermark >= eventTime + retentionMs.
            if (e.getValue() + retentionMs <= st.watermark) {
                it.remove();
                evicted++;
            }
        }
        return evicted;
    }

    // ---------- migration ----------

    /** Bump the routing version (ownership change). Returns the new version. */
    public long bumpRoutingVersion() {
        return ++routingVersion;
    }

    /** Export one partition's dedup state so it can move with its keys. */
    public PartitionSnapshot exportPartition(int partition, long version) {
        checkVersion(version);
        PartitionState st = partitions[partition];
        synchronized (st) {
            return new PartitionSnapshot(partition, routingVersion, st.watermark, st.maxEventTime,
                    new LinkedHashMap<>(st.entries));
        }
    }

    /**
     * Import a partition snapshot on the node taking ownership. The snapshot's
     * routing version must not be older than this node's current version; the
     * node adopts the snapshot's version so that both sides agree afterwards.
     */
    public void importPartition(PartitionSnapshot snap) {
        if (snap.routingVersion() < routingVersion) {
            throw new StaleRoutingVersionException(routingVersion, snap.routingVersion());
        }
        routingVersion = snap.routingVersion();
        PartitionState st = partitions[snap.partition()];
        synchronized (st) {
            st.watermark = Math.max(st.watermark, snap.watermark());
            st.maxEventTime = Math.max(st.maxEventTime, snap.maxEventTime());
            snap.entries().forEach((id, t) -> st.entries.merge(id, t, Math::max));
            // Entries that are already past retention under the adopted watermark
            // are dropped immediately so memory stays bounded.
            advanceLocked(st, st.watermark);
        }
    }

    // ---------- introspection (used by /state and tests) ----------

    public int entryCount(int partition) {
        PartitionState st = partitions[partition];
        synchronized (st) { return st.entries.size(); }
    }

    public long watermark(int partition) {
        PartitionState st = partitions[partition];
        synchronized (st) { return st.watermark; }
    }

    public int totalEntryCount() {
        int n = 0;
        for (int i = 0; i < numPartitions; i++) n += entryCount(i);
        return n;
    }

    public Map<String, Object> describe() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("routingVersion", routingVersion);
        m.put("numPartitions", numPartitions);
        m.put("allowedLatenessMs", allowedLatenessMs);
        m.put("retentionMs", retentionMs);
        List<Object> ps = new ArrayList<>();
        for (int i = 0; i < numPartitions; i++) {
            PartitionState st = partitions[i];
            synchronized (st) {
                Map<String, Object> pm = new LinkedHashMap<>();
                pm.put("partition", i);
                pm.put("watermark", st.watermark == Long.MIN_VALUE ? null : st.watermark);
                pm.put("entries", st.entries.size());
                ps.add(pm);
            }
        }
        m.put("partitions", ps);
        return m;
    }

    // ---------- snapshot <-> JSON ----------

    public static Map<String, Object> snapshotToJson(PartitionSnapshot snap) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("partition", snap.partition());
        m.put("routingVersion", snap.routingVersion());
        m.put("watermark", snap.watermark());
        m.put("maxEventTime", snap.maxEventTime());
        m.put("entries", new LinkedHashMap<>(snap.entries()));
        return m;
    }

    @SuppressWarnings("unchecked")
    public static PartitionSnapshot snapshotFromJson(Map<String, Object> m) {
        int partition = ((Number) m.get("partition")).intValue();
        long version = ((Number) m.get("routingVersion")).longValue();
        long watermark = ((Number) m.get("watermark")).longValue();
        long maxEventTime = ((Number) m.get("maxEventTime")).longValue();
        Map<String, Long> entries = new LinkedHashMap<>();
        Object e = m.get("entries");
        if (e instanceof Map) {
            ((Map<String, Object>) e).forEach((k, v) -> entries.put(k, ((Number) v).longValue()));
        }
        return new PartitionSnapshot(partition, version, watermark, maxEventTime, entries);
    }
}
