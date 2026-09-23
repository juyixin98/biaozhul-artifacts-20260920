package dedup;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * State of one partition key: routing epoch (fencing token), event-time
 * watermark, retention window, and the retained event-id set.
 *
 * All mutating/reading operations are synchronized on the partition itself;
 * the service additionally serializes partition-map changes at its own level.
 *
 * Retention semantics (inclusive boundary): an id anchored at event time
 * {@code t} is expired once {@code watermark >= t + retentionMillis}. An id
 * seen again with a later event time lifts the anchor, so its retention can
 * only be extended, never shortened by an out-of-order repeat.
 */
final class Partition {

    enum State {
        ACTIVE,
        MIGRATING
    }

    private final String key;
    private long epoch;
    private long retentionMillis;
    private long watermark = Long.MIN_VALUE; // no watermark observed yet
    private State state = State.ACTIVE;
    private boolean drained = false; // true after the source finishes a handoff

    // eventId -> entry. Only ids with anchorEventTime + retention > watermark are kept.
    private final LinkedHashMap<String, DedupEntry> entries = new LinkedHashMap<>();

    Partition(String key, long epoch, long retentionMillis) {
        this.key = key;
        this.epoch = epoch;
        this.retentionMillis = retentionMillis;
    }

    String key() {
        return key;
    }

    synchronized long epoch() {
        return epoch;
    }

    synchronized long watermark() {
        return watermark;
    }

    synchronized long retentionMillis() {
        return retentionMillis;
    }

    synchronized boolean isMigrating() {
        return state == State.MIGRATING;
    }

    synchronized int entryCount() {
        return entries.size();
    }

    long entryExpiry(DedupEntry e) {
        return e.anchorEventTime() + retentionMillis;
    }

    private boolean isExpired(DedupEntry e, long wm) {
        return wm != Long.MIN_VALUE && wm >= entryExpiry(e);
    }

    /**
     * Check and record an event.
     *
     * @return a result describing whether the event is new/duplicate and
     *         whether it arrived already past the retention window (late).
     */
    synchronized CheckResult check(String eventId, long eventTime) {
        requireActive();

        // The event is "late" if, by the current watermark, anything anchored at
        // this event time is already beyond retention. It is still delivered
        // (event-time semantics can't forbid it), but must never be stored:
        // storing it would create an entry that is expired the instant it lands.
        boolean late = watermark != Long.MIN_VALUE
                && watermark >= eventTime + retentionMillis;

        DedupEntry existing = entries.get(eventId);
        if (existing != null) {
            // A retained entry is always unexpired, hence this is a true repeat.
            existing.liftAnchorTo(eventTime);
            return CheckResult.duplicate(late);
        }

        if (!late) {
            entries.put(eventId, new DedupEntry(eventId, eventTime));
        }
        return CheckResult.fresh(late);
    }

    /**
     * Advance the watermark. Must be monotonic; never goes backwards.
     * Advancing purges every entry whose retention window has fully elapsed,
     * which is what bounds memory as the stream progresses.
     */
    synchronized long advanceWatermark(long newWatermark) {
        requireActive();
        if (newWatermark < watermark) {
            throw new ApiException(ErrorCode.WATERMARK_MONOTONIC,
                    "Watermark cannot move backwards: current=" + watermark
                            + ", requested=" + newWatermark);
        }
        watermark = newWatermark;
        purgeExpired();
        return watermark;
    }

    private void purgeExpired() {
        if (watermark == Long.MIN_VALUE || entries.isEmpty()) {
            return;
        }
        entries.values().removeIf(e -> isExpired(e, watermark));
    }

    // ---------------------------------------------------------------
    // Migration handoff
    // ---------------------------------------------------------------

    /**
     * Freeze the partition and export its state. While MIGRATING every event
     * and watermark write is rejected, closing the dual-write window.
     */
    synchronized Snapshot exportSnapshot() {
        if (state == State.MIGRATING) {
            throw new ApiException(ErrorCode.PARTITION_MIGRATING,
                    "Partition '" + key + "' is already in MIGRATING state");
        }
        purgeExpired();
        List<Snapshot.EntryView> views = new ArrayList<>(entries.size());
        for (DedupEntry e : entries.values()) {
            views.add(new Snapshot.EntryView(e.eventId(), e.anchorEventTime()));
        }
        state = State.MIGRATING;
        return new Snapshot(key, epoch, watermark, retentionMillis, views);
    }

    /** Roll back a freeze without changing any data. */
    synchronized void abortMigration() {
        state = State.ACTIVE;
    }

    /**
     * Install a snapshot coming from a source partition (used on the destination).
     *
     * The snapshot's partitionKey records where the state came from; in a real
     * cluster it equals this key, but in the single-node demo the destination
     * is registered under a separate key to emulate a new owner, so equality is
     * not enforced here. The epoch fence is what matters.
     */
    synchronized void installSnapshot(Snapshot snap, long newEpoch) {
        if (state == State.MIGRATING) {
            throw new ApiException(ErrorCode.PARTITION_MIGRATING,
                    "Partition '" + key + "' is MIGRATING; cannot install snapshot");
        }
        if (newEpoch <= snap.epoch()) {
            // The whole point of the epoch fence: a stale or replayed snapshot
            // carrying an old routing version must be refused.
            throw new ApiException(ErrorCode.INVALID_ROUTING_VERSION,
                    "New epoch " + newEpoch + " must be greater than snapshot epoch "
                            + snap.epoch());
        }
        this.epoch = newEpoch;
        this.watermark = snap.watermark();
        this.retentionMillis = snap.retentionMillis();
        this.entries.clear();
        for (Snapshot.EntryView v : snap.entries()) {
            this.entries.put(v.eventId(), new DedupEntry(v.eventId(), v.anchorEventTime()));
        }
        purgeExpired();
        state = State.ACTIVE;
    }

    /** Source side: mark the cutover complete, retire the routing epoch and drop state. */
    synchronized void completeMigration(long observedEpoch) {
        if (state != State.MIGRATING) {
            throw new ApiException(ErrorCode.PARTITION_MIGRATING,
                    "Partition '" + key + "' is not in MIGRATING state");
        }
        if (observedEpoch != epoch) {
            throw new ApiException(ErrorCode.INVALID_ROUTING_VERSION,
                    "Routing version mismatch: expected epoch " + epoch
                            + " but request carried " + observedEpoch);
        }
        entries.clear();
        watermark = Long.MIN_VALUE;
        // Retire the old routing version: any late producer still routing here
        // with the old epoch is fenced. The partition stays as an empty tombstone.
        epoch++;
        drained = true;
        state = State.ACTIVE;
    }

    synchronized Map<String, Object> statusView() {
        Map<String, Object> view = new LinkedHashMap<>();
        view.put("partitionKey", key);
        view.put("epoch", epoch);
        view.put("state", state.name());
        view.put("drained", drained);
        view.put("watermark", watermark == Long.MIN_VALUE ? null : watermark);
        view.put("retentionMillis", retentionMillis);
        view.put("retainedEventCount", entries.size());
        // Approximate footprint of the retained id set, for verification of
        // memory release; exact heap accounting is JVM-internal.
        long chars = 0;
        for (DedupEntry e : entries.values()) {
            chars += e.eventId().length();
        }
        view.put("retainedIdChars", chars);
        return view;
    }

    private void requireActive() {
        if (state == State.MIGRATING) {
            throw new ApiException(ErrorCode.PARTITION_MIGRATING,
                    "Partition '" + key + "' is MIGRATING (epoch " + epoch
                            + "); writes are rejected until the handoff completes");
        }
        if (drained) {
            throw new ApiException(ErrorCode.INVALID_ROUTING_VERSION,
                    "Partition '" + key + "' has been drained to a new owner (epoch "
                            + epoch + "); this replica no longer accepts writes");
        }
    }

    record CheckResult(boolean duplicate, boolean late) {
        static CheckResult fresh(boolean late) {
            return new CheckResult(false, late);
        }

        static CheckResult duplicate(boolean late) {
            return new CheckResult(true, late);
        }
    }
}
