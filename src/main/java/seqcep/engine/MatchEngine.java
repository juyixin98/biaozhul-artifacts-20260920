package seqcep.engine;

import seqcep.json.Json;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Stateful A&rarr;B&rarr;C sequence matcher.
 *
 * <h1>Matching rules</h1>
 * <ul>
 *   <li>Events are grouped by {@code entityId}; state never crosses entity boundaries.</li>
 *   <li>For one entity, a match is any triple of events A, B, C with
 *       {@code tsA &lt;= tsB &lt;= tsC} (same timestamps tie-broken by input seq) and
 *       {@code tsC - tsA &lt;= WINDOW_MS} (10&nbsp;seconds, boundary inclusive).</li>
 *   <li>Irrelevant events (any type other than A/B/C) are skipped and never affect state.</li>
 *   <li>All combinations are enumerated: each pending A pairs with each new B, each pending
 *       A&middot;B pair pairs with each new C. Partial state is <b>not</b> consumed, so
 *       overlapping matches share events (e.g. A,A,B,C yields 2 matches).</li>
 *   <li>Partial chains older than the window (measured from A) are expired eagerly on the
 *       next event for the entity.</li>
 * </ul>
 *
 * <h1>Durability</h1>
 * Every accepted event is appended and fsynced to the {@link Wal} before it changes matcher
 * state. On {@link #open(String, Path)} the log is replayed in order, deterministically
 * rebuilding partial state and the full match list.
 */
public final class MatchEngine {

    public static final long WINDOW_MS = 10_000L;

    /** One A waiting for a B. */
    private record PendingA(Event a) {}

    /** One A&middot;B pair waiting for a C; the chain deadline is measured from {@code a}. */
    private record PendingAB(Event a, Event b) {}

    private static final class EntityState {
        final List<PendingA> openA = new ArrayList<>();
        final List<PendingAB> openAB = new ArrayList<>();
        long matchCount;
    }

    private final String engineId;
    private Wal wal; // null = in-memory mode (tests only); rotated by reset()
    private final Path walPath;

    private final Map<String, EntityState> entities = new LinkedHashMap<>();
    private final List<Match> matches = new ArrayList<>();
    private long lastSeq;
    private long acceptedEvents;
    private boolean broken;

    private MatchEngine(String engineId, Wal wal, Path walPath) {
        this.engineId = engineId;
        this.wal = wal;
        this.walPath = walPath;
    }

    /** Opens (or creates) a persisted engine and replays the log. */
    public static MatchEngine open(String engineId, Path walPath) {
        Wal w = Wal.open(walPath);
        MatchEngine engine = new MatchEngine(engineId, w, walPath);
        List<Event> recovered = Wal.replay(walPath);
        for (Event e : recovered) {
            engine.apply(e);
        }
        return engine;
    }

    /** Non-durable engine used by unit tests. */
    public static MatchEngine inMemory(String engineId) {
        return new MatchEngine(engineId, null, null);
    }

    public String engineId() {
        return engineId;
    }

    // ------------------------------------------------------------- ingest

    /**
     * Accepts one event: assigns its global input seq, durably appends it, then updates
     * matcher state. Returns the event as stored (with assigned seq).
     *
     * @throws IllegalStateException if the WAL previously failed; the process must restart
     *         and replay rather than accepting possibly-unlogged events
     */
    public synchronized Event ingest(String type, String entityId, long timestamp) {
        if (broken) {
            throw new IllegalStateException(
                    "Engine WAL is in a failed state; restart required before ingesting more events");
        }
        Event e = new Event(nextSeq(), type, entityId, timestamp);
        if (wal != null) {
            // append() runs before apply(): a crash/failure here leaves no unlogged state.
            try {
                wal.append(e);
            } catch (RuntimeException ex) {
                broken = true;
                throw ex;
            }
        }
        acceptedEvents = Math.max(acceptedEvents, e.seq());
        apply(e);
        return e;
    }

    private long nextSeq() {
        return ++lastSeq;
    }

    /** Updates matcher state for an already-durable event; also used during WAL replay. */
    private void apply(Event e) {
        // seq bookkeeping must advance identically on replay even for irrelevant events
        lastSeq = Math.max(lastSeq, e.seq());
        acceptedEvents = Math.max(acceptedEvents, e.seq());

        EntityState st = entities.computeIfAbsent(e.entityId(), k -> new EntityState());
        sweep(st, e.timestamp());

        switch (e.type()) {
            case "A" -> st.openA.add(new PendingA(e));
            case "B" -> {
                // Every non-expired A with tsA <= tsB starts a new partial pair.
                // Seq order is guaranteed by processing order; equal timestamps tie-break on seq.
                for (PendingA pa : st.openA) {
                    if (pa.a.timestamp() <= e.timestamp()) {
                        st.openAB.add(new PendingAB(pa.a, e));
                    }
                }
            }
            case "C" -> {
                // Every non-expired A·B pair with tsB <= tsC completes a match.
                // Pairs are retained afterwards to allow overlapping matches.
                for (PendingAB p : st.openAB) {
                    if (p.b.timestamp() <= e.timestamp()) {
                        Match m = new Match(e.entityId(),
                                p.a.seq(), p.b.seq(), e.seq(),
                                p.a.timestamp(), p.b.timestamp(), e.timestamp());
                        matches.add(m);
                        st.matchCount++;
                    }
                }
            }
            default -> {
                // Irrelevant event type: explicitly skipped, matcher state untouched.
            }
        }
    }

    /** Drops partial chains whose A is already older than the window at time {@code now}. */
    private void sweep(EntityState st, long now) {
        st.openA.removeIf(pa -> now - pa.a.timestamp() > WINDOW_MS);
        st.openAB.removeIf(p -> now - p.a.timestamp() > WINDOW_MS);
    }

    // ------------------------------------------------------------- queries

    public synchronized List<Match> matches(String entityId) {
        List<Match> out = new ArrayList<>();
        for (Match m : matches) {
            if (entityId == null || entityId.equals(m.entityId())) {
                out.add(m);
            }
        }
        out.sort(Match::comparePresentation);
        return out;
    }

    public synchronized int matchCount(String entityId) {
        if (entityId == null) return matches.size();
        EntityState st = entities.get(entityId);
        return st == null ? 0 : (int) st.matchCount;
    }

    /** Full partial-state snapshot, used for recovery verification and /state. */
    public synchronized Map<String, Object> snapshot() {
        Map<String, Object> out = Json.obj(
                "engineId", engineId,
                "windowMs", WINDOW_MS,
                "lastSeq", lastSeq,
                "acceptedEvents", acceptedEvents,
                "matchCount", (long) matches.size());
        Map<String, Object> ents = new LinkedHashMap<>();
        for (Map.Entry<String, EntityState> en : entities.entrySet()) {
            EntityState st = en.getValue();
            Map<String, Object> em = new LinkedHashMap<>();
            em.put("openA", st.openA.stream().map(pa -> partial(pa.a)).toList());
            em.put("openAB", st.openAB.stream()
                    .map(p -> Json.obj("a", partial(p.a), "b", partial(p.b))).toList());
            em.put("matchCount", st.matchCount);
            ents.put(en.getKey(), em);
        }
        out.put("entities", ents);
        return out;
    }

    private static Map<String, Object> partial(Event e) {
        return Json.obj("seq", e.seq(), "type", e.type(), "ts", e.timestamp());
    }

    /**
     * Structural fingerprint of the partial-match state: equal fingerprints after restart
     * prove the "partial match state consistent" acceptance criterion. Completed matches are
     * rebuilt too and covered separately by the match list.
     */
    public synchronized String partialStateFingerprint() {
        StringBuilder sb = new StringBuilder();
        sb.append("lastSeq=").append(lastSeq).append(';');
        List<String> keys = new ArrayList<>(entities.keySet());
        keys.sort(String::compareTo);
        for (String key : keys) {
            EntityState st = entities.get(key);
            sb.append(key).append(":A[");
            st.openA.stream().map(p -> p.a.seq()).sorted()
                    .forEach(s -> sb.append(s).append(','));
            sb.append("]AB[");
            st.openAB.stream()
                    .map(p -> p.a.seq() + "-" + p.b.seq())
                    .sorted()
                    .forEach(s -> sb.append(s).append(','));
            sb.append("]M=").append(st.matchCount).append(';');
        }
        return sb.toString();
    }

    // ------------------------------------------------------------- reset / close

    /** Clears all matcher state and rotates the WAL to an empty, fresh log. */
    public synchronized void reset() {
        if (wal != null) {
            wal.close();
            try {
                Files.deleteIfExists(walPath);
            } catch (IOException e) {
                throw new WalCorruptionException("Failed to delete WAL during reset", e);
            }
            wal = Wal.open(walPath);
        }
        entities.clear();
        matches.clear();
        lastSeq = 0;
        acceptedEvents = 0;
        broken = false;
    }

    public synchronized Path walPath() {
        return walPath;
    }

    public synchronized void close() {
        if (wal != null) {
            wal.close();
            wal = null;
        }
    }
}
