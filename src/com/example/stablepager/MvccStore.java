package com.example.stablepager;

import java.util.ArrayList;
import java.util.Collection;
import java.util.List;
import java.util.Map;
import java.util.NavigableMap;
import java.util.Optional;
import java.util.TreeMap;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;

/**
 * In-memory MVCC store plus a registry of named snapshots.
 *
 * <p>Every mutation bumps a monotonically increasing version number. A snapshot
 * captures the version, creation time and a <em>defensive copy</em> of all rows,
 * so later inserts/deletes/updates never disturb a page walk pinned to an old
 * snapshot. Each use refreshes the snapshot's TTL; a background janitor removes
 * snapshots that have been idle longer than {@code ttlMillis}, after which an old
 * cursor fails explicitly with {@code SNAPSHOT_EXPIRED}.
 */
public final class MvccStore {

    /** Immutable read view of one version. */
    public static final class Snapshot {
        private final String id;
        private final long version;
        private final long createdAt;
        private final Map<String, Item> rows; // immutable snapshot copy

        Snapshot(String id, long version, long createdAt, Map<String, Item> rows) {
            this.id = id;
            this.version = version;
            this.createdAt = createdAt;
            this.rows = rows;
        }

        public String id() {
            return id;
        }

        public long version() {
            return version;
        }

        public long createdAt() {
            return createdAt;
        }

        public Optional<Item> find(String id) {
            return Optional.ofNullable(rows.get(id));
        }

        public Collection<Item> rows() {
            return rows.values();
        }
    }

    private static final class Entry {
        final Snapshot snapshot;
        volatile long lastTouched;

        Entry(Snapshot snapshot, long lastTouched) {
            this.snapshot = snapshot;
            this.lastTouched = lastTouched;
        }
    }

    private final NavigableMap<String, Item> live = new TreeMap<>();
    private long version = 0;
    private final ConcurrentHashMap<String, Entry> snapshots = new ConcurrentHashMap<>();
    private final long ttlMillis;
    private final ScheduledExecutorService janitor;

    public MvccStore(long ttlMillis) {
        this.ttlMillis = ttlMillis;
        this.janitor = Executors.newSingleThreadScheduledExecutor(r -> {
            Thread t = new Thread(r, "stablepager-snapshot-janitor");
            t.setDaemon(true);
            return t;
        });
        long period = Math.max(50, ttlMillis / 2);
        this.janitor.scheduleAtFixedRate(this::sweep, period, period, TimeUnit.MILLISECONDS);
    }

    /** Stops the background janitor. */
    public void shutdown() {
        janitor.shutdownNow();
    }

    public long ttlMillis() {
        return ttlMillis;
    }

    // ------------------------------------------------------------------
    // Mutations (serialized; each one bumps the version)
    // ------------------------------------------------------------------

    public synchronized Item insert(String id, String name, String category, long score) {
        if (live.containsKey(id)) {
            throw new ApiException(409, "ID_CONFLICT", "an item with id '" + id + "' already exists");
        }
        long now = System.currentTimeMillis();
        Item item = new Item(id, name, category, score, now, now);
        live.put(id, item);
        version++;
        return item;
    }

    public synchronized Item update(String id, String name, String category, Long score) {
        Item current = live.get(id);
        if (current == null) {
            throw new ApiException(404, "NOT_FOUND", "no item with id '" + id + "'");
        }
        Item updated = current.withPatch(name, category, score, System.currentTimeMillis());
        live.put(id, updated);
        version++;
        return updated;
    }

    public synchronized void delete(String id) {
        if (live.remove(id) == null) {
            throw new ApiException(404, "NOT_FOUND", "no item with id '" + id + "'");
        }
        version++;
    }

    public synchronized Optional<Item> getLive(String id) {
        return Optional.ofNullable(live.get(id));
    }

    /** Creates a fresh snapshot of the current version. */
    public synchronized Snapshot createSnapshot() {
        long now = System.currentTimeMillis();
        String id = UUID.randomUUID().toString().replace("-", "");
        Map<String, Item> copy = Map.copyOf(live);
        Snapshot snapshot = new Snapshot(id, version, now, copy);
        snapshots.put(id, new Entry(snapshot, now));
        return snapshot;
    }

    /**
     * Resolves a snapshot id and refreshes its TTL. Throws
     * {@code SNAPSHOT_EXPIRED} when it is unknown or has already been swept.
     */
    public Snapshot resolve(String id) {
        Entry entry = snapshots.get(id);
        long now = System.currentTimeMillis();
        if (entry == null || now - entry.lastTouched > ttlMillis) {
            if (entry != null) {
                snapshots.remove(id, entry);
            }
            throw new ApiException(410, "SNAPSHOT_EXPIRED",
                    "snapshot '" + id + "' has expired or does not exist; restart the walk from the first page");
        }
        entry.lastTouched = now;
        return entry.snapshot;
    }

    private void sweep() {
        long now = System.currentTimeMillis();
        for (Map.Entry<String, Entry> e : snapshots.entrySet()) {
            Entry entry = e.getValue();
            if (now - entry.lastTouched > ttlMillis) {
                snapshots.remove(e.getKey(), entry);
            }
        }
    }
}
