package com.example.segindex;

import com.fasterxml.jackson.databind.ObjectMapper;

import java.io.IOException;
import java.io.UncheckedIOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.locks.ReentrantLock;

/**
 * Facade tying everything together: a buffered writer, atomic commits,
 * snapshot readers and a background merge scheduler.
 *
 * <p>Concurrency model: a single lock guards the manifest and all manifest swaps.
 * Segment files are immutable and readers load them fully into memory while
 * holding the lock, so readers can never observe a partially deleted file.
 */
public final class Index implements AutoCloseable {

    static final String SEGMENT_PREFIX = "seg_";

    private final Path dir;
    private final IndexConfig config;
    private final ManifestStore store;
    private final ReentrantLock lock = new ReentrantLock();

    private volatile Manifest manifest;
    private final List<SegmentMerger.IndexedDoc> pendingAdds = new ArrayList<>();
    private final List<Tombstone> pendingTombstones = new ArrayList<>();
    /** Latest generation per docId, including uncommitted writes. */
    private Map<String, Long> generations;
    /** In-memory segment name allocator; persisted value may lag, never leads. */
    private long nextSegmentSeq;
    /** Serializes concurrent merges (background scheduler vs. forceMerge). */
    private final Object mergeLock = new Object();
    private final MergeScheduler merger;
    private volatile boolean closed;

    private Index(Path dir, IndexConfig config, ManifestStore store, Manifest manifest) {
        this.dir = dir;
        this.config = config;
        this.store = store;
        this.manifest = manifest;
        this.generations = new HashMap<>(manifest.generations);
        this.nextSegmentSeq = manifest.nextSegmentSeq;
        this.merger = new MergeScheduler(this);
    }

    public static Index open(Path dir, IndexConfig config) {
        try {
            Files.createDirectories(dir);
        } catch (IOException e) {
            throw new UncheckedIOException("cannot create index dir " + dir, e);
        }
        ManifestStore store = new ManifestStore(dir);
        Manifest manifest = store.load();
        store.cleanupOrphans(manifest);
        Index index = new Index(dir, config, store, manifest);
        if (config.backgroundMergeEnabled()) {
            index.merger.start();
        }
        return index;
    }

    // ---- writes ----

    /**
     * Buffers a new generation of {@code id}; visible to new readers after {@link #commit()}.
     * Re-adding an existing id is an update: all previous generations are
     * tombstoned, so terms that only existed in older versions stop matching.
     */
    public long addDocument(String id, String text) {
        lock.lock();
        try {
            ensureOpen();
            long generation = generations.getOrDefault(id, 0L) + 1;
            generations.put(id, generation);
            if (generation > 1) {
                pendingTombstones.add(new Tombstone(id, generation - 1));
            }
            pendingAdds.add(new SegmentMerger.IndexedDoc(id, generation, text));
            return generation;
        } finally {
            lock.unlock();
        }
    }

    /** Buffers a delete of all current generations of {@code id}. Returns false if id unknown. */
    public boolean deleteDocument(String id) {
        lock.lock();
        try {
            ensureOpen();
            Long gen = generations.get(id);
            if (gen == null) {
                return false;
            }
            pendingTombstones.add(new Tombstone(id, gen));
            return true;
        } finally {
            lock.unlock();
        }
    }

    /** Flushes buffered writes to a new segment and atomically publishes a new manifest. */
    public void commit() {
        lock.lock();
        try {
            ensureOpen();
            if (pendingAdds.isEmpty() && pendingTombstones.isEmpty()) {
                return;
            }
            Manifest next = manifest.copy();
            if (!pendingAdds.isEmpty()) {
                String name = allocateSegmentNameLocked();
                SegmentWriter.write(dir, name, SegmentMerger.buildPostings(pendingAdds),
                        SegmentMerger.buildDocStore(pendingAdds), store.mapper(), config.crashHook());
                next.segments.add(name);
            }
            next.tombstones.addAll(pendingTombstones);
            next.generations = new LinkedHashMap<>(generations);
            next.nextSegmentSeq = nextSegmentSeq;
            config.crashHook().accept("commit.beforeManifestCommit");
            store.save(next);
            manifest = next;
            pendingAdds.clear();
            pendingTombstones.clear();
        } finally {
            lock.unlock();
        }
        merger.signal();
    }

    // ---- reads ----

    /** Opens a consistent snapshot reader over the currently published segments. */
    public IndexReader newReader() {
        lock.lock();
        try {
            ensureOpen();
            List<SegmentReader> readers = new ArrayList<>();
            for (String name : manifest.segments) {
                readers.add(SegmentReader.open(dir, name, store.mapper()));
            }
            return new IndexReader(readers, manifest.tombstones);
        } finally {
            lock.unlock();
        }
    }

    /** Triggers a synchronous merge of all live segments. */
    public void forceMerge() {
        List<String> all;
        lock.lock();
        try {
            ensureOpen();
            all = new ArrayList<>(manifest.segments);
        } finally {
            lock.unlock();
        }
        synchronized (mergeLock) {
            // Re-read under the merge lock: a racing merge may have already merged them.
            List<String> current = manifestSnapshot().segments;
            List<String> stillLive = new ArrayList<>(all);
            stillLive.retainAll(current);
            new SegmentMerger(this).merge(stillLive);
        }
    }

    /** Current manifest state (defensive copy) for status endpoints and tests. */
    public Manifest manifestSnapshot() {
        lock.lock();
        try {
            return manifest.copy();
        } finally {
            lock.unlock();
        }
    }

    // ---- hooks used by SegmentMerger / MergeScheduler ----

    String allocateSegmentName() {
        lock.lock();
        try {
            return allocateSegmentNameLocked();
        } finally {
            lock.unlock();
        }
    }

    /** Caller must hold {@link #lock}. */
    private String allocateSegmentNameLocked() {
        return SEGMENT_PREFIX + String.format("%06d", nextSegmentSeq++);
    }

    SegmentReader openSegment(String name) {
        lock.lock();
        try {
            return SegmentReader.open(dir, name, store.mapper());
        } finally {
            lock.unlock();
        }
    }

    /**
     * Atomically replaces {@code mergedAway} with {@code newSegment} in the
     * manifest, then deletes the old segment directories. If the merge
     * covered every live segment, all tombstones have been applied and are
     * dropped.
     */
    void publishMerge(List<String> mergedAway, String newSegment) {
        lock.lock();
        try {
            if (closed) {
                return;
            }
            Manifest next = manifest.copy();
            next.nextSegmentSeq = nextSegmentSeq;
            List<String> remaining = new ArrayList<>(next.segments);
            if (!remaining.containsAll(mergedAway)) {
                // A racing merge already consumed some inputs; discard this result.
                ManifestStore.deleteRecursively(dir.resolve(newSegment));
                return;
            }
            remaining.removeAll(mergedAway);
            remaining.add(newSegment);
            next.segments = remaining;
            if (remaining.size() == 1 && mergedAway.containsAll(manifest.segments)) {
                next.tombstones = new ArrayList<>();
            }
            config.crashHook().accept("merge.beforeManifestCommit");
            store.save(next);
            manifest = next;
            for (String old : mergedAway) {
                ManifestStore.deleteRecursively(dir.resolve(old));
            }
        } catch (IOException e) {
            throw new UncheckedIOException("failed to publish merge", e);
        } finally {
            lock.unlock();
        }
    }

    int liveSegmentCount() {
        return manifestSnapshot().segments.size();
    }

    Path directory() {
        return dir;
    }

    IndexConfig config() {
        return config;
    }

    ObjectMapper mapper() {
        return store.mapper();
    }

    private void ensureOpen() {
        if (closed) {
            throw new IllegalStateException("index is closed");
        }
    }

    @Override
    public void close() {
        lock.lock();
        try {
            closed = true;
        } finally {
            lock.unlock();
        }
        merger.shutdown();
    }
}
