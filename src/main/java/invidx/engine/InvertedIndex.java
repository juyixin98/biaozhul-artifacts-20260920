package invidx.engine;

import invidx.catalog.Manifest;
import invidx.catalog.ManifestStore;
import invidx.deletes.DeleteLog;
import invidx.merge.SegmentMerger;
import invidx.model.DocKey;
import invidx.model.Document;
import invidx.search.Hit;
import invidx.search.Query;
import invidx.search.Snapshot;
import invidx.search.Searcher;
import invidx.segment.Segment;
import invidx.segment.SegmentInfo;
import invidx.segment.SegmentReader;
import invidx.segment.SegmentWriter;
import invidx.store.Directory;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.Set;
import java.util.TreeMap;
import java.util.concurrent.locks.ReentrantLock;
import java.util.concurrent.atomic.AtomicLong;
import java.util.stream.Collectors;

/**
 * The segmented inverted-index engine.
 *
 * <p>Concurrency model: one {@link ReentrantLock} guards all metadata
 * transitions. Each transition publishes a fresh immutable
 * {@link IndexState} through the state field, so a query either runs
 * entirely against the old state or entirely against the new one — reads
 * never observe a half-applied flush or merge.
 *
 * <p>Durability ordering (crash safety):
 * <ol>
 *   <li>killing a published revision: tombstone+generation records are
 *       fsynced to deletes.log <b>before</b> any state changes;</li>
 *   <li>publishing a segment: write+fsync a uniquely-named temp file,
 *       atomic rename to {@code seg_NNNNNN.seg}, then atomically swap
 *       {@code manifest.json} — the manifest is the single commit point;</li>
 *   <li>merge sources are deleted only after the manifest names the merged
 *       segment; an earlier crash leaves only unreferenced orphans which
 *       {@link #open} removes.</li>
 * </ol>
 */
public final class InvertedIndex implements AutoCloseable {

    private final Directory directory;
    private final ManifestStore manifestStore;
    private final DeleteLog deleteLog;
    private final IndexConfig config;
    private final CrashHook crashHook;
    private final ReentrantLock lock = new ReentrantLock();
    private final MergeScheduler scheduler;
    private final AtomicLong mergeBuildSeq = new AtomicLong();

    private volatile IndexState state;
    private volatile boolean closed;

    private InvertedIndex(Directory directory, IndexConfig config, CrashHook crashHook) {
        this.directory = directory;
        this.manifestStore = new ManifestStore(directory);
        this.deleteLog = new DeleteLog(directory);
        this.config = config;
        this.crashHook = crashHook == null ? CrashHook.NOOP : crashHook;
        this.scheduler = new MergeScheduler(config, this::mergeTick);
    }

    // ------------------------------------------------------------------ open

    public static InvertedIndex open(Path root) throws IOException {
        return open(root, IndexConfig.defaults(), CrashHook.NOOP);
    }

    public static InvertedIndex open(Path root, IndexConfig config, CrashHook crashHook)
            throws IOException {
        InvertedIndex index = new InvertedIndex(new Directory(root), config, crashHook);
        index.recover();
        index.scheduler.start();
        return index;
    }

    private void recover() throws IOException {
        manifestStore.cleanupTemp();

        Manifest manifest = manifestStore.exists() ? manifestStore.read() : null;
        DeleteLog.Replay replay = deleteLog.open();

        List<SegmentInfo> infos = manifest == null ? List.of() : manifest.segments();
        List<Segment> loaded = new ArrayList<>();
        for (SegmentInfo info : infos) {
            loaded.add(SegmentReader.load(directory, info.name(), info.merged()));
        }

        // Generation counters: take the strongest of manifest, kill log, data.
        Map<Integer, Long> nextGen = new HashMap<>(
                manifest == null ? Map.of() : manifest.nextGen());
        replay.genAdvances().forEach((id, g) -> nextGen.merge(id, g, Math::max));
        int maxId = -1;
        for (Segment seg : loaded) {
            for (Map.Entry<Integer, Segment.Entry> e : seg.docs().entrySet()) {
                nextGen.merge(e.getKey(), e.getValue().gen() + 1, Math::max);
                maxId = Math.max(maxId, e.getKey());
            }
        }
        int nextDocId = manifest == null ? 0 : manifest.nextDocId();
        nextDocId = Math.max(nextDocId, maxId + 1);

        cleanupOrphanFiles(infos);

        long seq = manifest == null ? 0 : manifest.segmentSeq();
        long version = manifest == null ? 0 : manifest.version();
        state = new IndexState(loaded, new HashSet<>(replay.tombstones()),
                nextGen, nextDocId, seq, version, new RamBuffer());
    }

    private void cleanupOrphanFiles(List<SegmentInfo> referenced) throws IOException {
        Path segDir = directory.resolve("segments");
        if (!Files.isDirectory(segDir)) {
            return;
        }
        Set<String> keep = referenced.stream()
                .map(s -> s.name() + ".seg")
                .collect(Collectors.toSet());
        try (var stream = Files.list(segDir)) {
            for (Path p : (Iterable<Path>) stream::iterator) {
                if (!keep.contains(p.getFileName().toString())) {
                    directory.deleteRecursively(p);
                }
            }
        }
    }

    // ------------------------------------------------------------- mutations

    /** Add with an auto-assigned id; returns the assigned key. */
    public DocKey addDocument(String text) {
        lock.lock();
        try {
            int id = state.nextDocId;
            return putLocked(id, text, true);
        } finally {
            lock.unlock();
        }
    }

    /** Add or replace a document revision for an explicit id. */
    public DocKey putDocument(int id, String text) {
        if (id < 0 || id > 0x7FFFFFFF) {
            throw new IllegalArgumentException("id out of range: " + id);
        }
        Objects.requireNonNull(text, "text");
        lock.lock();
        try {
            return putLocked(id, text, id >= state.nextDocId);
        } finally {
            lock.unlock();
        }
    }

    private DocKey putLocked(int id, String text, boolean advanceDocId) {
        IndexState s = state;
        long newGen = s.nextGen.getOrDefault(id, 1L);

        boolean ramOnly = s.ram.docById(id) != null;
        if (!ramOnly && isPublishedLive(s, id)) {
            long oldGen = latestPublishedGen(s, id);
            try {
                deleteLog.appendKill(id, oldGen, newGen + 1);
            } catch (IOException e) {
                throw new IndexIOException("failed to append tombstone for id=" + id, e);
            }
            Set<Long> deleted = new HashSet<>(s.deleted);
            deleted.add(DocKey.encode(id, oldGen));
            s = new IndexState(s.segments, deleted, s.nextGen, s.nextDocId,
                    s.segmentSeq, s.version, s.ram);
        }

        Map<Integer, Long> nextGen = new HashMap<>(s.nextGen);
        nextGen.put(id, newGen + 1);
        int nextDocId = Math.max(s.nextDocId, advanceDocId ? id + 1 : s.nextDocId);

        Document doc = new Document(id, newGen, text);
        RamBuffer ram = s.ram;
        ram.add(doc);

        if (ram.size() < config.maxBufferedDocs()) {
            state = new IndexState(s.segments, s.deleted, nextGen, nextDocId,
                    s.segmentSeq, s.version, ram);
            return doc.key();
        }
        flushLocked(s, nextGen, nextDocId);
        return doc.key();
    }

    /** Delete the current live revision of an id. Returns false if none exists. */
    public boolean deleteDocument(int id) {
        lock.lock();
        try {
            IndexState s = state;
            Document ramDoc = s.ram.docById(id);
            if (ramDoc != null) {
                s.ram.removeById(id);
                state = new IndexState(s.segments, s.deleted, s.nextGen, s.nextDocId,
                        s.segmentSeq, s.version, s.ram);
                return true;
            }
            if (!isPublishedLive(s, id)) {
                return false;
            }
            long oldGen = latestPublishedGen(s, id);
            long nextGen = Math.max(s.nextGen.getOrDefault(id, 1L), oldGen + 1);
            try {
                deleteLog.appendKill(id, oldGen, nextGen);
            } catch (IOException e) {
                throw new IndexIOException("failed to append tombstone for id=" + id, e);
            }
            Set<Long> deleted = new HashSet<>(s.deleted);
            deleted.add(DocKey.encode(id, oldGen));
            Map<Integer, Long> gens = new HashMap<>(s.nextGen);
            gens.put(id, nextGen);
            state = new IndexState(s.segments, deleted, gens, s.nextDocId,
                    s.segmentSeq, s.version, s.ram);
            return true;
        } finally {
            lock.unlock();
        }
    }

    // ----------------------------------------------------------------- flush

    /** Drain buffered documents into a new published segment. */
    public void flush() {
        lock.lock();
        try {
            if (state.ram.isEmpty()) {
                return;
            }
            flushLocked(state, new HashMap<>(state.nextGen), state.nextDocId);
        } finally {
            lock.unlock();
        }
    }

    /**
     * Serialize the RAM buffer, publish a new segment and commit the
     * manifest. The buffer is drained only after the commit succeeds, so a
     * storage failure does not lose buffered documents.
     */
    private void flushLocked(IndexState s, Map<Integer, Long> nextGen, int nextDocId) {
        List<Document> docs = s.ram.docsSnapshot().values().stream()
                .sorted(Comparator.comparing(Document::key))
                .toList();
        if (docs.isEmpty()) {
            return;
        }
        long seq = s.segmentSeq + 1;
        String name = segmentName(seq);
        byte[] data = SegmentWriter.serialize(docs);

        Path tmp;
        try {
            tmp = SegmentWriter.writeTemp(directory, name, data);
            crashHook.onPoint("flush.tmp");
            directory.atomicMove(tmp, SegmentReader.segmentPath(directory, name));
            crashHook.onPoint("flush.rename");
        } catch (IOException e) {
            throw new IndexIOException("failed to publish segment " + name, e);
        }

        Segment segment = SegmentReader.parse(name, false, data);
        List<Segment> segments = new ArrayList<>(s.segments);
        segments.add(segment);
        long version = s.version + 1;
        try {
            manifestStore.write(version, seq, nextDocId, nextGen, toInfos(segments));
            crashHook.onPoint("flush.manifest");
        } catch (IOException e) {
            throw new IndexIOException("failed to commit manifest for " + name, e);
        }
        s.ram.drain();
        state = new IndexState(segments, s.deleted, nextGen, nextDocId,
                seq, version, new RamBuffer());
    }

    // ----------------------------------------------------------------- merge

    /**
     * Merge a handful of the smallest segments when there are enough of
     * them, or when the smallest segments carry tombstones worth
     * reclaiming. Returns the number of merged segments (0 if nothing ran).
     */
    public int maybeMerge() {
        lock.lock();
        List<Segment> chosen;
        Set<Long> deletedAtBuild;
        try {
            if (!scheduler.claim()) {
                return 0;
            }
            chosen = pickMergeSegments(state);
            if (chosen.size() < 2) {
                scheduler.release();
                return 0;
            }
            deletedAtBuild = Set.copyOf(state.deleted);
        } finally {
            lock.unlock();
        }
        try {
            runMerge(chosen, deletedAtBuild);
            return chosen.size();
        } finally {
            scheduler.release();
        }
    }

    /** Merge every currently published segment into one. */
    public int forceMerge() {
        lock.lock();
        List<Segment> chosen;
        Set<Long> deletedAtBuild;
        try {
            if (state.segments.size() <= 1) {
                return 0;
            }
            chosen = List.copyOf(state.segments);
            deletedAtBuild = Set.copyOf(state.deleted);
        } finally {
            lock.unlock();
        }
        runMerge(chosen, deletedAtBuild);
        return chosen.size();
    }

    private List<Segment> pickMergeSegments(IndexState s) {
        List<Segment> sorted = s.segments.stream()
                .sorted(Comparator.comparingInt(Segment::docCount)
                        .thenComparing(Segment::name))
                .limit(config.mergeFactor())
                .toList();
        boolean enoughSegments = s.segments.size() >= config.mergeFactor();
        boolean reclaimable = sorted.stream().anyMatch(seg -> hasTombstone(seg, s.deleted));
        if (sorted.size() < 2 || (!enoughSegments && !reclaimable)) {
            return List.of();
        }
        return sorted;
    }

    private boolean hasTombstone(Segment seg, Set<Long> deleted) {
        for (Map.Entry<Integer, Segment.Entry> e : seg.docs().entrySet()) {
            if (deleted.contains(DocKey.encode(e.getKey(), e.getValue().gen()))) {
                return true;
            }
        }
        return false;
    }

    private void runMerge(List<Segment> sources, Set<Long> deletedAtBuild) {
        if (sources.size() < 2) {
            return;
        }
        Set<String> sourceNames = sources.stream().map(Segment::name).collect(Collectors.toSet());

        // The temp file gets a build-unique name: a flush committing while
        // this merge builds must not collide with it. The final segment
        // number is allocated under the write lock at commit time.
        String buildName = "merge_build_" + System.nanoTime() + "_" + mergeBuildSeq.incrementAndGet();
        SegmentMerger.MergePlan plan;
        try {
            plan = SegmentMerger.build(directory, buildName, sources, deletedAtBuild);
            crashHook.onPoint("merge.tmp");
        } catch (IOException e) {
            throw new IndexIOException("merge build failed for " + buildName, e);
        }

        lock.lock();
        try {
            IndexState s = state;
            boolean stillPresent = s.segments.stream()
                    .map(Segment::name).collect(Collectors.toSet())
                    .containsAll(sourceNames);
            if (!stillPresent) {
                deleteQuietly(plan.tmpFile());
                return;
            }

            long seq = s.segmentSeq + 1;
            String targetName = segmentName(seq);
            Path finalPath = SegmentReader.segmentPath(directory, targetName);
            try {
                directory.atomicMove(plan.tmpFile(), finalPath);
                crashHook.onPoint("merge.rename");
            } catch (IOException e) {
                throw new IndexIOException("merge rename failed for " + targetName, e);
            }
            Segment merged;
            try {
                merged = SegmentReader.load(directory, targetName, true);
            } catch (IOException e) {
                throw new IndexIOException("failed to read freshly merged " + targetName, e);
            }

            List<Segment> remaining = s.segments.stream()
                    .filter(seg -> !sourceNames.contains(seg.name()))
                    .collect(Collectors.toCollection(ArrayList::new));
            remaining.add(merged);
            remaining.sort(Comparator.comparing(Segment::name));

            long version = s.version + 1;
            try {
                manifestStore.write(version, seq, s.nextDocId, s.nextGen, toInfos(remaining));
                crashHook.onPoint("merge.manifest");
            } catch (IOException e) {
                throw new IndexIOException("merge manifest commit failed for " + targetName, e);
            }
            state = new IndexState(remaining, s.deleted, s.nextGen, s.nextDocId,
                    seq, version, s.ram);
        } finally {
            lock.unlock();
        }

        // Source files are unreferenced now; delete outside the lock.
        // A crash here is harmless: recovery removes unreferenced files.
        for (String name : sourceNames) {
            deleteQuietly(SegmentReader.segmentPath(directory, name));
        }
    }

    private void deleteQuietly(Path path) {
        try {
            directory.deleteIfExists(path);
        } catch (IOException ignored) {
            // Startup cleanup is the durable safety net.
        }
    }

    private void mergeTick() {
        try {
            maybeMerge();
        } catch (SimulatedCrash crash) {
            throw crash;
        } catch (RuntimeException e) {
            System.getLogger(InvertedIndex.class.getName())
                    .log(System.Logger.Level.WARNING, "background merge failed", e);
        }
    }

    // ----------------------------------------------------------------- query

    public Snapshot snapshot() {
        lock.lock();
        try {
            IndexState s = state;
            List<Segment> view = new ArrayList<>(s.segments.size() + 1);
            view.addAll(s.segments);

            Map<Integer, Document> ramDocs = s.ram.docsSnapshot();
            if (!ramDocs.isEmpty()) {
                Map<Integer, Segment.Entry> ramEntries = new HashMap<>();
                ramDocs.values().forEach(d ->
                        ramEntries.put(d.id(), new Segment.Entry(d.gen(), d.text())));
                TreeMap<String, long[]> ramPostings = new TreeMap<>(s.ram.postingsSnapshot());
                view.add(Segment.inMemory("__ram", ramEntries, ramPostings));
            }

            Map<Integer, Snapshot.LiveDoc> latest = new HashMap<>();
            for (Segment seg : s.segments) {
                for (Map.Entry<Integer, Segment.Entry> e : seg.docs().entrySet()) {
                    long gen = e.getValue().gen();
                    Snapshot.LiveDoc current = latest.get(e.getKey());
                    if (current == null || gen > current.gen()) {
                        latest.put(e.getKey(),
                                new Snapshot.LiveDoc(gen, e.getValue().text(), seg.name()));
                    }
                }
            }
            ramDocs.values().forEach(d ->
                    latest.put(d.id(), new Snapshot.LiveDoc(d.gen(), d.text(), "__ram")));
            latest.entrySet().removeIf(e ->
                    s.deleted.contains(DocKey.encode(e.getKey(), e.getValue().gen())));

            return new Snapshot(s.version, view, s.deleted, latest, !ramDocs.isEmpty());
        } finally {
            lock.unlock();
        }
    }

    public List<Hit> search(Query query) {
        return new Searcher(snapshot()).search(query);
    }

    public List<Hit> search(String querySpec) {
        return search(Query.parse(querySpec));
    }

    public Snapshot.LiveDoc get(int id) {
        return snapshot().latest().get(id);
    }

    // ------------------------------------------------------------------ stats

    public IndexStats stats() {
        lock.lock();
        try {
            Snapshot snap = snapshot();
            List<String> diskNames = snap.segments().stream()
                    .map(Segment::name)
                    .filter(n -> !n.equals("__ram"))
                    .toList();
            return new IndexStats(diskNames.size(), diskNames,
                    snap.deleted().size(), state.ram.size(),
                    state.segmentSeq, state.version, snap.docCount());
        } finally {
            lock.unlock();
        }
    }

    @Override
    public void close() {
        lock.lock();
        try {
            if (closed) {
                return;
            }
            closed = true;
            scheduler.stop();
            flush();
        } finally {
            lock.unlock();
        }
    }

    // ------------------------------------------------------------- internals

    private boolean isPublishedLive(IndexState s, int id) {
        long gen = latestPublishedGen(s, id);
        return gen >= 0 && !s.deleted.contains(DocKey.encode(id, gen));
    }

    private long latestPublishedGen(IndexState s, int id) {
        long best = -1;
        for (Segment seg : s.segments) {
            Segment.Entry e = seg.get(id);
            if (e != null && e.gen() > best) {
                best = e.gen();
            }
        }
        return best;
    }

    private static List<SegmentInfo> toInfos(List<Segment> segments) {
        return segments.stream()
                .map(seg -> new SegmentInfo(seg.name(), seg.docCount(), seg.merged()))
                .toList();
    }

    private static String segmentName(long seq) {
        return String.format("seg_%06d", seq);
    }
}
