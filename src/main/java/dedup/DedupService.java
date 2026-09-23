package dedup;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * In-memory registry of partitions and the entry point for all domain
 * operations. Partition lookup is synchronized; per-partition operations are
 * synchronized on the partition object.
 *
 * Routing-version fencing: every write may carry the epoch the client believes
 * it is talking to. A mismatch (stale producer after a migration, replayed
 * snapshot, etc.) is rejected with {@link ErrorCode#INVALID_ROUTING_VERSION}.
 */
public final class DedupService {

    private final Map<String, Partition> partitions = new LinkedHashMap<>();

    public synchronized Map<String, Object> createPartition(String key,
                                                            long epoch,
                                                            long retentionMillis) {
        if (partitions.containsKey(key)) {
            throw new ApiException(ErrorCode.PARTITION_EXISTS,
                    "Partition '" + key + "' already exists");
        }
        if (retentionMillis <= 0) {
            throw ApiException.invalidBody("retentionMillis must be positive");
        }
        Partition p = new Partition(key, epoch, retentionMillis);
        partitions.put(key, p);
        return p.statusView();
    }

    public synchronized List<String> listPartitions() {
        return new ArrayList<>(partitions.keySet());
    }

    public synchronized Map<String, Object> status(String key) {
        return require(key).statusView();
    }

    public Map<String, Object> checkEvent(String key, Long expectedEpoch,
                                          String eventId, long eventTime) {
        Partition p = require(key);
        fence(p, expectedEpoch);
        Partition.CheckResult result = p.check(eventId, eventTime);

        Map<String, Object> view = new LinkedHashMap<>();
        view.put("partitionKey", key);
        synchronized (p) {
            view.put("epoch", p.epoch());
        }
        view.put("eventId", eventId);
        view.put("eventTime", eventTime);
        view.put("status", result.duplicate() ? "DUPLICATE" : "NEW");
        view.put("duplicate", result.duplicate());
        view.put("late", result.late());
        return view;
    }

    public Map<String, Object> advanceWatermark(String key, Long expectedEpoch, long newWatermark) {
        Partition p = require(key);
        fence(p, expectedEpoch);
        long wm = p.advanceWatermark(newWatermark);
        return p.statusView();
    }

    // ---------------------------------------------------------------
    // Migration
    // ---------------------------------------------------------------

    public Map<String, Object> exportSnapshot(String key, Long expectedEpoch) {
        Partition p = require(key);
        fence(p, expectedEpoch);
        Snapshot snap = p.exportSnapshot();
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("partition", p.statusView());
        body.put("snapshot", snapView(snap));
        return body;
    }

    public Map<String, Object> importSnapshot(String key, Long expectedEpoch,
                                              Snapshot snapshot, long newEpoch) {
        Partition p;
        synchronized (this) {
            p = partitions.get(key);
            if (p == null) {
                // Auto-create the destination at epoch 0 so a brand-new owner
                // can receive a handoff; installSnapshot then raises the epoch.
                if (snapshot.retentionMillis() <= 0) {
                    throw ApiException.invalidBody("snapshot.retentionMillis must be positive");
                }
                p = new Partition(key, 0, snapshot.retentionMillis());
                partitions.put(key, p);
            }
        }
        fence(p, expectedEpoch);
        p.installSnapshot(snapshot, newEpoch);
        return p.statusView();
    }

    public Map<String, Object> abortMigration(String key, Long expectedEpoch) {
        Partition p = require(key);
        fence(p, expectedEpoch);
        p.abortMigration();
        return p.statusView();
    }

    public Map<String, Object> completeMigration(String key, Long expectedEpoch) {
        Partition p = require(key);
        fence(p, expectedEpoch);
        p.completeMigration(expectedEpoch);
        return p.statusView();
    }

    public synchronized void deletePartition(String key, Long expectedEpoch) {
        Partition p = require(key);
        fence(p, expectedEpoch);
        partitions.remove(key);
    }

    // ---------------------------------------------------------------

    private Partition require(String key) {
        Partition p;
        synchronized (this) {
            p = partitions.get(key);
        }
        if (p == null) {
            throw new ApiException(ErrorCode.NOT_FOUND, "Partition '" + key + "' not found");
        }
        return p;
    }

    private void fence(Partition p, Long expectedEpoch) {
        if (expectedEpoch != null && expectedEpoch != p.epoch()) {
            throw new ApiException(ErrorCode.INVALID_ROUTING_VERSION,
                    "Routing version rejected: partition '" + p.key() + "' is at epoch "
                            + p.epoch() + " but request carried epoch " + expectedEpoch);
        }
    }

    static Map<String, Object> snapView(Snapshot snap) {
        Map<String, Object> view = new LinkedHashMap<>();
        view.put("partitionKey", snap.partitionKey());
        view.put("epoch", snap.epoch());
        view.put("watermark", snap.watermark() == Long.MIN_VALUE ? null : snap.watermark());
        view.put("retentionMillis", snap.retentionMillis());
        List<Map<String, Object>> entries = new ArrayList<>();
        for (Snapshot.EntryView e : snap.entries()) {
            Map<String, Object> ev = new LinkedHashMap<>();
            ev.put("eventId", e.eventId());
            ev.put("anchorEventTime", e.anchorEventTime());
            entries.add(ev);
        }
        view.put("entries", entries);
        return view;
    }

    /** Reconstruct a snapshot from a parsed JSON body. */
    @SuppressWarnings("unchecked")
    public static Snapshot snapshotFromBody(Map<String, Object> body) {
        Object snapObj = body.get("snapshot");
        // Tolerate being handed the full export response ({partition, snapshot}).
        if (snapObj instanceof Map<?, ?> wrapper && wrapper.get("snapshot") instanceof Map<?, ?>) {
            snapObj = wrapper.get("snapshot");
        }
        if (!(snapObj instanceof Map<?, ?> raw)) {
            throw ApiException.invalidBody("Missing object field 'snapshot'");
        }
        Map<String, Object> snap = (Map<String, Object>) raw;
        String key = Json.reqString(snap, "partitionKey");
        long epoch = Json.reqLong(snap, "epoch");
        // Watermark may be null when the source never observed one.
        Object wmRaw = snap.get("watermark");
        long watermark;
        if (wmRaw == null) {
            watermark = Long.MIN_VALUE;
        } else {
            watermark = Json.asLong(wmRaw, "snapshot.watermark");
        }
        long retention = Json.reqLong(snap, "retentionMillis");
        if (retention <= 0) {
            throw ApiException.invalidBody("snapshot.retentionMillis must be positive");
        }
        Object entriesObj = snap.get("entries");
        if (!(entriesObj instanceof List<?> rawList)) {
            throw ApiException.invalidBody("snapshot.entries must be an array");
        }
        List<Snapshot.EntryView> entries = new ArrayList<>();
        for (Object item : rawList) {
            if (!(item instanceof Map<?, ?> emRaw)) {
                throw ApiException.invalidBody("each snapshot entry must be an object");
            }
            Map<String, Object> em = (Map<String, Object>) emRaw;
            entries.add(new Snapshot.EntryView(
                    Json.reqString(em, "eventId"),
                    Json.reqLong(em, "anchorEventTime")));
        }
        return new Snapshot(key, epoch, watermark, retention, entries);
    }
}
