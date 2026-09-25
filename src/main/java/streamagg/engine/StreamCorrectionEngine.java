package streamagg.engine;

import streamagg.model.Aggregate;
import streamagg.model.Event;
import streamagg.model.IngestOp;
import streamagg.model.IngestResult;
import streamagg.model.IngestStatus;
import streamagg.model.OpType;
import streamagg.model.OutputSnapshot;
import streamagg.model.ResolvedOp;
import streamagg.time.Clock;
import streamagg.time.SystemClock;

import java.math.BigDecimal;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.Set;
import java.util.TreeMap;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.CopyOnWriteArrayList;
import java.time.Duration;

/**
 * Streaming retraction/correction aggregation engine.
 *
 * <p>Maintains, per grouping key, the running {@code sum} and {@code count} of live
 * events while supporting out-of-order lifecycle operations:
 *
 * <ul>
 *   <li><b>ADD</b>: a new event by ID.</li>
 *   <li><b>RETRACT</b>: an undo — removes a previously added event (or one that will
 *       be added later: the retract is buffered as a pending dependency).</li>
 *   <li><b>CORRECT</b>: changes an event's key and/or value; corrections may arrive
 *       before the ADD they target and are buffered until the dependency resolves.</li>
 * </ul>
 *
 * <h2>Ordering model</h2>
 * Every event ID has its own 1-based version chain. Operations carrying an explicit
 * {@code version} must resolve as 1,2,3,…; a gap causes buffering (v3 before v1/v2
 * is parked until they arrive). Operations without a version resolve in arrival
 * order; a RETRACT/CORRECT for an event not yet ADDed is buffered at the head and
 * released by the next legal ADD.
 *
 * <h2>Idempotency</h2>
 * Replays carrying a previously seen {@code opId} are answered
 * {@link IngestStatus#DUPLICATE} and change nothing. Without an opId, a structural
 * fingerprint over (eventId, op, version, key, value) suppresses identical replays.
 *
 * <h2>Invariants</h2>
 * A key's {@code count} never goes negative — the engine guards the invariant and
 * throws if an internal path would violate it.
 */
public final class StreamCorrectionEngine {

    /** Reacts to emitted output snapshots (periodic emission). */
    public interface OutputListener {
        void onOutput(OutputSnapshot snapshot);
    }

    private final Clock clock;
    private final Duration allowedLateness;
    private final Map<String, IdState> ids = new HashMap<>();
    // Per-key aggregates
    private final Map<String, Aggregate> aggregates = new LinkedHashMap<>();
    // Append-only ledger of resolved lifecycle changes (resolution order)
    private final List<ResolvedOp> ledger = new ArrayList<>();
    // Idempotency
    private final Set<String> seenOpIds = ConcurrentHashMap.newKeySet();
    // Diagnostics
    private final Map<String, IngestStatus> terminalByOpId = new LinkedHashMap<>();
    private final List<String> warnings = new ArrayList<>();
    // Event-time watermark (max eventTime seen), null until an event time arrives
    private Long watermark;
    private long outputSequence;
    private final List<OutputListener> listeners = new CopyOnWriteArrayList<>();

    private StreamCorrectionEngine(Builder b) {
        this.clock = b.clock;
        this.allowedLateness = b.allowedLateness;
    }

    public static Builder builder() {
        return new Builder();
    }

    /** Convenience: engine with system clock and no lateness cutoff. */
    public static StreamCorrectionEngine createDefault() {
        return builder().build();
    }

    public static final class Builder {
        private Clock clock = SystemClock.INSTANCE;
        private Duration allowedLateness = null; // null = never reject as late

        public Builder clock(Clock clock) {
            this.clock = clock;
            return this;
        }

        /** Reject operations with eventTime older than watermark - lateness. Default: never reject. */
        public Builder allowedLateness(Duration d) {
            this.allowedLateness = d;
            return this;
        }

        public StreamCorrectionEngine build() {
            return new StreamCorrectionEngine(this);
        }
    }

    // ---------------------------------------------------------------------
    // Ingest
    // ---------------------------------------------------------------------

    /** Feeds one operation; thread-safe against other ingest/snapshot calls. */
    public synchronized IngestResult ingest(IngestOp op) {
        IngestResult validation = validate(op);
        if (validation != null) {
            return validation;
        }

        // Global opId idempotency — same token is never applied twice.
        if (op.opId() != null && (seenOpIds.contains(op.opId()) || findPendingOpId(op.eventId(), op.opId()) != null)) {
            return IngestResult.duplicate("opId '" + op.opId() + "' already seen");
        }

        // Watermark / late handling
        if (op.eventTime() != null) {
            if (watermark != null && allowedLateness != null
                    && op.eventTime() < watermark - allowedLateness.toMillis()) {
                return IngestResult.late("eventTime " + op.eventTime()
                        + " is past watermark " + watermark + " minus allowed lateness");
            }
            if (watermark == null || op.eventTime() > watermark) {
                watermark = op.eventTime();
            }
        }

        IdState st = ids.computeIfAbsent(op.eventId(), k -> new IdState());

        if (op.version() != null) {
            return ingestVersioned(st, op);
        }
        return ingestUnversioned(st, op);
    }

    private IngestResult ingestVersioned(IdState st, IngestOp op) {
        long v = op.version();

        // Structural replay idempotency for versioned ops already resolved.
        ResolvedOp applied = findLedgerEntry(op.eventId(), v);
        if (applied != null) {
            if (sameAsApplied(applied, op)) {
                return IngestResult.duplicate("version " + v + " already applied with identical content");
            }
            return IngestResult.conflict("version " + v + " already applied with different content");
        }
        if (st.pendingVersioned.containsKey(v)) {
            IngestOp parked = st.pendingVersioned.get(v);
            if (sameContent(parked, op, v)) {
                return IngestResult.duplicate("identical version " + v + " already buffered");
            }
            return IngestResult.conflict("version " + v + " already buffered with different content");
        }
        if (v < st.nextVersion) {
            return IngestResult.conflict("version " + v + " is below next expected version " + st.nextVersion);
        }

        // The chain terminated in a terminal RETRACT: no versioned CORRECT/RETRACT can
        // follow it (a fresh ADD opens a new, unversioned chain instead).
        if (st.consumed && st.current == null) {
            return IngestResult.conflict("event '" + op.eventId()
                    + "' was retired by a RETRACT at version " + (st.nextVersion - 1)
                    + "; cannot apply " + op.op() + " at version " + v
                    + " (ADD again to open a new chain)");
        }

        if (v > st.nextVersion) {
            // Out-of-order: park until the gap fills.
            st.pendingVersioned.put(v, op);
            rememberOpId(op);
            return IngestResult.buffered("waiting for version " + st.nextVersion + " before version " + v,
                    totalPending());
        }

        // v == nextVersion: legal next step (legality depends on lifecycle, checked at apply).
        st.pendingVersioned.put(v, op);
        rememberOpId(op);
        return drain(st, op.eventId());
    }

    private IngestResult ingestUnversioned(IdState st, IngestOp op) {
        // A brand new chain (or a chain fully retired by a terminal RETRACT) starts fresh.
        boolean freshChain = st.current == null && st.nextVersion == 1 && !st.consumed;
        boolean afterRetire = st.consumed && st.current == null && st.pendingVersioned.isEmpty()
                && st.pendingUnversioned.isEmpty();

        switch (op.op()) {
            case ADD -> {
                if (freshChain || afterRetire) {
                    if (afterRetire) {
                        // A re-ADD after a complete add-then-retract lifecycle opens a new chain.
                        st.consumed = false;
                        st.nextVersion = 1;
                    }
                    if (seenFingerprintUnversioned(st, op)) {
                        return IngestResult.duplicate("identical ADD already processed/buffered");
                    }
                    // If a RETRACT/CORRECT is already waiting head-of-chain for this event,
                    // this ADD must jump ahead of it: the dependency releases the waiters.
                    if (!st.pendingUnversioned.isEmpty()
                            && st.pendingUnversioned.get(0).op() != OpType.ADD) {
                        st.pendingUnversioned.add(0, op);
                    } else {
                        st.pendingUnversioned.add(op);
                    }
                    rememberOpId(op);
                    return drain(st, op.eventId());
                }
                // Live event already exists: an unversioned ADD is a no-op idempotent upsert
                // only when identical; otherwise a conflict (use CORRECT to change it).
                if (sameLiveEvent(st.current, op)) {
                    return IngestResult.duplicate("identical ADD for live event already present");
                }
                return IngestResult.conflict("event already exists; use CORRECT to change key/value");
            }
            case RETRACT, CORRECT -> {
                if (st.current == null) {
                    // Dependency not present yet — buffer at the head; a future ADD releases it.
                    if (seenFingerprintUnversioned(st, op)) {
                        return IngestResult.duplicate("identical " + op.op() + " already buffered/processed");
                    }
                    st.pendingUnversioned.add(op);
                    rememberOpId(op);
                    return IngestResult.buffered(op.op() + " buffered: waiting for ADD of event '"
                            + op.eventId() + "'", totalPending());
                }
                if (seenFingerprintUnversioned(st, op)) {
                    return IngestResult.duplicate("identical " + op.op() + " already processed");
                }
                st.pendingUnversioned.add(op);
                rememberOpId(op);
                return drain(st, op.eventId());
            }
        }
        return IngestResult.invalid("unhandled op type: " + op.op());
    }

    /**
     * Applies everything ready for one event ID: the next explicit version and any
     * consecutive chain, then any unversioned head operations (RETRACT/CORRECT
     * buffered before an ADD get the versions the chain releases).
     */
    private IngestResult drain(IdState st, String eventId) {
        List<ResolvedOp> resolved = new ArrayList<>();

        // Phase 1: explicit-version chain.
        while (true) {
            IngestOp op = st.pendingVersioned.remove(st.nextVersion);
            if (op == null) {
                break;
            }
            String legality = checkLegality(st, op);
            if (legality != null) {
                // Illegal step: record conflict, skip this version so later versions can resolve.
                st.nextVersion++;
                long conflictedVersion = st.nextVersion - 1;
                warnings.add(eventId + " v" + conflictedVersion + " rejected: " + legality);
                if (op.opId() != null) {
                    terminalByOpId.put(op.opId(), IngestStatus.CONFLICT);
                }
                continue;
            }
            resolved.add(applyOne(st, op, st.nextVersion));
            st.nextVersion++;
            if (st.current == null) {
                // Terminal RETRACT: any versions parked above it can never resolve on
                // this chain — reject them as conflicts rather than parking forever.
                st.consumed = true;
                for (IngestOp parked : new ArrayList<>(st.pendingVersioned.values())) {
                    st.pendingVersioned.remove(parked.version());
                    warnings.add(eventId + " v" + parked.version() + " rejected: chain retired by RETRACT");
                    if (parked.opId() != null) {
                        terminalByOpId.put(parked.opId(), IngestStatus.CONFLICT);
                    }
                }
                break;
            }
        }

        // Phase 2: unversioned head operations (drain in arrival order).
        while (st.current != null && !st.pendingUnversioned.isEmpty()) {
            IngestOp op = st.pendingUnversioned.remove(0);
            String legality = checkLegality(st, op);
            if (legality != null) {
                warnings.add(eventId + " unversioned op rejected: " + legality);
                if (op.opId() != null) {
                    terminalByOpId.put(op.opId(), IngestStatus.CONFLICT);
                }
                // Advance version for the skipped slot so chains stay consistent.
                st.nextVersion++;
                continue;
            }
            resolved.add(applyOne(st, op, st.nextVersion));
            st.nextVersion++;
        }

        // A buffered unversioned ADD waiting behind versioned ops lands here.
        while (st.current == null && !st.consumed && !st.pendingUnversioned.isEmpty()) {
            IngestOp op = st.pendingUnversioned.get(0);
            if (op.op() != OpType.ADD) {
                break; // head RETRACT/CORRECT still waiting for an ADD
            }
            st.pendingUnversioned.remove(0);
            resolved.add(applyOne(st, op, st.nextVersion));
            st.nextVersion++;
            // Newly added: drain any RETRACT/CORRECT queued behind it.
            while (st.current != null && !st.pendingUnversioned.isEmpty()) {
                IngestOp next = st.pendingUnversioned.remove(0);
                String legality = checkLegality(st, next);
                if (legality != null) {
                    warnings.add(eventId + " unversioned op rejected: " + legality);
                    if (next.opId() != null) {
                        terminalByOpId.put(next.opId(), IngestStatus.CONFLICT);
                    }
                    st.nextVersion++;
                    continue;
                }
                resolved.add(applyOne(st, next, st.nextVersion));
                st.nextVersion++;
            }
            if (st.current == null) {
                st.consumed = true;
            }
        }

        if (resolved.isEmpty()) {
            // The triggering op was parked out-of-order; callers of this method parked it themselves.
            return IngestResult.buffered("buffered pending dependencies", totalPending());
        }
        return IngestResult.applied(resolved.get(0), resolved, totalPending());
    }

    /** Applies one known-legal operation, mutating aggregates, event state, ledger. */
    private ResolvedOp applyOne(IdState st, IngestOp op, long version) {
        Event cur = st.current;
        String oldKey = cur == null ? null : cur.key();
        BigDecimal oldValue = cur == null ? null : cur.value();
        long now = clock.instant().toEpochMilli();

        String newKey = null;
        BigDecimal newValue = null;

        switch (op.op()) {
            case ADD -> {
                newKey = op.key();
                newValue = op.value();
                aggregates.computeIfAbsent(newKey, k -> new Aggregate()).add(newValue);
                st.current = new Event(op.eventId(), newKey, newValue, version, now, now);
            }
            case RETRACT -> {
                newKey = null;
                newValue = null;
                Aggregate agg = aggregates.get(oldKey);
                agg.remove(oldValue);
                agg.assertNoNegativeDrift();
                cleanupAggregateIfEmpty(oldKey, agg);
                st.current = null;
            }
            case CORRECT -> {
                newKey = op.key() != null ? op.key() : oldKey;
                newValue = op.value() != null ? op.value() : oldValue;
                if (!newKey.equals(oldKey)) {
                    aggregates.computeIfAbsent(newKey, k -> new Aggregate()).add(newValue);
                    Aggregate oldAgg = aggregates.get(oldKey);
                    oldAgg.remove(oldValue);
                    oldAgg.assertNoNegativeDrift();
                    cleanupAggregateIfEmpty(oldKey, oldAgg);
                } else if (newValue.compareTo(oldValue) != 0) {
                    Aggregate agg = aggregates.get(oldKey);
                    agg.remove(oldValue);
                    agg.add(newValue);
                }
                st.current = new Event(op.eventId(), newKey, newValue, version, cur.createdAt(), now);
            }
        }

        ResolvedOp r = new ResolvedOp(op.opId(), op.eventId(), op.op(), version,
                oldKey, oldValue, newKey, newValue, op.eventTime(), now);
        ledger.add(r);
        if (op.opId() != null) {
            terminalByOpId.put(op.opId(), IngestStatus.APPLIED);
        }
        return r;
    }

    private void cleanupAggregateIfEmpty(String key, Aggregate agg) {
        if (agg.count() == 0 && agg.sum().signum() == 0) {
            aggregates.remove(key);
        }
    }

    // ---------------------------------------------------------------------
    // Validation / dedupe helpers
    // ---------------------------------------------------------------------

    private IngestResult validate(IngestOp op) {
        if (op == null) {
            return IngestResult.invalid("operation is null");
        }
        if (op.eventId() == null || op.eventId().isBlank()) {
            return IngestResult.invalid("eventId is required");
        }
        if (op.op() == null) {
            return IngestResult.invalid("op is required");
        }
        if (op.version() != null && op.version() < 1) {
            return IngestResult.invalid("version must be >= 1");
        }
        if (op.op() == OpType.ADD) {
            if (op.key() == null || op.key().isBlank()) {
                return IngestResult.invalid("ADD requires key");
            }
            if (op.value() == null) {
                return IngestResult.invalid("ADD requires value");
            }
        }
        if (op.op() == OpType.CORRECT && op.key() == null && op.value() == null) {
            return IngestResult.invalid("CORRECT requires a new key and/or value");
        }
        return null;
    }

    /** Lifecycle legality relative to the chain's current position (null = legal). */
    private String checkLegality(IdState st, IngestOp op) {
        boolean live = st.current != null;
        return switch (op.op()) {
            case ADD -> live
                    ? "ADD on an already live event (id=" + op.eventId() + ")"
                    : (op.key() == null || op.value() == null ? "ADD needs key and value" : null);
            case RETRACT -> live ? null : "RETRACT with no live event";
            case CORRECT -> !live ? "CORRECT with no live event"
                    : (op.key() == null && op.value() == null ? "CORRECT needs a new key/value" : null);
        };
    }

    private void rememberOpId(IngestOp op) {
        if (op.opId() != null) {
            seenOpIds.add(op.opId());
        }
    }

    private IngestOp findPendingOpId(String eventId, String opId) {
        IdState st = ids.get(eventId);
        if (st == null) {
            return null;
        }
        for (IngestOp p : st.pendingVersioned.values()) {
            if (opId.equals(p.opId())) {
                return p;
            }
        }
        for (IngestOp p : st.pendingUnversioned) {
            if (opId.equals(p.opId())) {
                return p;
            }
        }
        return null;
    }

    private boolean sameContent(IngestOp parked, IngestOp op, long v) {
        return parked.op() == op.op()
                && Objects.equals(parked.key(), op.key())
                && Objects.equals(parked.value(), op.value())
                && Objects.equals(parked.eventTime(), op.eventTime())
                && op.version() == v;
    }

    /** Finds the resolved ledger entry for an event ID at a given version, if any. */
    private ResolvedOp findLedgerEntry(String eventId, long v) {
        // Ledger is append-only; per-event entries are few — linear scan is fine
        // for the small-data scale this library targets.
        for (int i = ledger.size() - 1; i >= 0; i--) {
            ResolvedOp r = ledger.get(i);
            if (r.eventId().equals(eventId) && r.version() == v) {
                return r;
            }
        }
        return null;
    }

    /** True when a replay operation matches exactly what resolved at its version. */
    private boolean sameAsApplied(ResolvedOp applied, IngestOp op) {
        if (applied.op() != op.op()) {
            return false;
        }
        return switch (op.op()) {
            case ADD -> Objects.equals(applied.newKey(), op.key())
                    && Objects.equals(applied.newValue(), op.value());
            case RETRACT -> true; // a RETRACT is fully identified by (eventId, version)
            case CORRECT -> Objects.equals(applied.newKey(), op.key() == null ? applied.newKey() : op.key())
                    && Objects.equals(applied.newValue(), op.value() == null ? applied.newValue() : op.value());
        };
    }

    private boolean sameLiveEvent(Event current, IngestOp op) {
        return current != null
                && Objects.equals(current.key(), op.key())
                && Objects.equals(current.value(), op.value());
    }

    /**
     * Fingerprint idempotency for ops without an explicit opId. Two structurally
     * identical unversioned lifecycle requests against the same event ID collapse;
     * this is what makes replaying an unversioned ADD/RETRACT stream safe.
     */
    private boolean seenFingerprintUnversioned(IdState st, IngestOp op) {
        for (IngestOp p : st.pendingUnversioned) {
            if (fingerprint(p).equals(fingerprint(op))) {
                return true;
            }
        }
        for (ResolvedOp r : ledger) {
            if (r.eventId().equals(op.eventId()) && r.op() == op.op()
                    && Objects.equals(r.newKey(), op.key())
                    && Objects.equals(r.newValue(), op.value())) {
                return true;
            }
        }
        return false;
    }

    private String fingerprint(IngestOp op) {
        return op.eventId() + "|" + op.op() + "|"
                + (op.key() == null ? "" : op.key()) + "|"
                + (op.value() == null ? "" : op.value().stripTrailingZeros().toPlainString());
    }

    // ---------------------------------------------------------------------
    // Reads
    // ---------------------------------------------------------------------

    public synchronized Map<String, Aggregate> aggregates() {
        return Collections.unmodifiableMap(aggregates);
    }

    public synchronized Map<String, Event> events() {
        Map<String, Event> out = new LinkedHashMap<>();
        for (Map.Entry<String, IdState> e : ids.entrySet()) {
            if (e.getValue().current != null) {
                out.put(e.getKey(), e.getValue().current);
            }
        }
        return out;
    }

    /** Pending buffered operations for one event ID (diagnostics). */
    public synchronized List<IngestOp> pendingFor(String eventId) {
        IdState st = ids.get(eventId);
        if (st == null) {
            return List.of();
        }
        List<IngestOp> out = new ArrayList<>(st.pendingVersioned.values());
        out.addAll(st.pendingUnversioned);
        return out;
    }

    public synchronized int totalPending() {
        int n = 0;
        for (IdState st : ids.values()) {
            n += st.pendingVersioned.size() + st.pendingUnversioned.size();
        }
        return n;
    }

    /** Event IDs that currently have buffered operations waiting on dependencies. */
    public synchronized List<String> eventIdsWithPending() {
        List<String> out = new ArrayList<>();
        for (Map.Entry<String, IdState> e : ids.entrySet()) {
            IdState st = e.getValue();
            if (!st.pendingVersioned.isEmpty() || !st.pendingUnversioned.isEmpty()) {
                out.add(e.getKey());
            }
        }
        return out;
    }

    public synchronized List<ResolvedOp> ledger() {
        return Collections.unmodifiableList(new ArrayList<>(ledger));
    }

    public synchronized Long watermark() {
        return watermark;
    }

    public synchronized List<String> warnings() {
        return List.copyOf(warnings);
    }

    // ---------------------------------------------------------------------
    // Output emission (scheduler-driven)
    // ---------------------------------------------------------------------

    public void addListener(OutputListener l) {
        listeners.add(l);
    }

    /** Builds and (when listeners exist) publishes an immutable output snapshot. */
    public synchronized OutputSnapshot emitSnapshot() {
        Map<String, BigDecimal> sums = new LinkedHashMap<>();
        Map<String, Long> counts = new LinkedHashMap<>();
        for (Map.Entry<String, Aggregate> e : aggregates.entrySet()) {
            sums.put(e.getKey(), e.getValue().sum());
            counts.put(e.getKey(), e.getValue().count());
        }
        int live = 0;
        for (IdState st : ids.values()) {
            if (st.current != null) {
                live++;
            }
        }
        List<ResolvedOp> recent = ledger.size() <= 20
                ? List.copyOf(ledger)
                : List.copyOf(ledger.subList(ledger.size() - 20, ledger.size()));
        OutputSnapshot snap = new OutputSnapshot(
                ++outputSequence, clock.instant().toEpochMilli(), watermark,
                sums, counts, live, totalPending(), recent);
        for (OutputListener l : listeners) {
            l.onOutput(snap);
        }
        return snap;
    }

    // ---------------------------------------------------------------------
    // Reset / replay
    // ---------------------------------------------------------------------

    public synchronized void reset() {
        ids.clear();
        aggregates.clear();
        ledger.clear();
        seenOpIds.clear();
        terminalByOpId.clear();
        warnings.clear();
        watermark = null;
        outputSequence = 0;
    }

    /**
     * Small-data exact reference implementation: recomputes per-key aggregates by
     * replaying the resolved ledger from scratch (the "final event ledger
     * recomputation" oracle). Streaming aggregates must equal this at all times.
     */
    public static Map<String, Aggregate> recomputeFromLedger(List<ResolvedOp> ledger) {
        Map<String, Aggregate> out = new LinkedHashMap<>();
        Map<String, ReEvent> live = new HashMap<>();
        for (ResolvedOp r : ledger) {
            switch (r.op()) {
                case ADD -> {
                    out.computeIfAbsent(r.newKey(), k -> new Aggregate()).add(r.newValue());
                    live.put(r.eventId(), new ReEvent(r.newKey(), r.newValue()));
                }
                case RETRACT -> {
                    ReEvent e = live.remove(r.eventId());
                    if (e == null) {
                        throw new IllegalStateException(
                                "negative count drift detected: RETRACT of event '"
                                        + r.eventId() + "' with no live event (ledger v" + r.version() + ")");
                    }
                    Aggregate agg = out.get(e.key());
                    if (agg == null) {
                        throw new IllegalStateException(
                                "negative count drift detected: key '" + e.key()
                                        + "' has no aggregate when retracting event '" + r.eventId() + "'");
                    }
                    agg.remove(e.value());
                    agg.assertNoNegativeDrift();
                    if (agg.count() == 0 && agg.sum().signum() == 0) {
                        out.remove(e.key());
                    }
                }
                case CORRECT -> {
                    ReEvent e = live.get(r.eventId());
                    if (e == null) {
                        throw new IllegalStateException(
                                "negative count drift detected: CORRECT of event '"
                                        + r.eventId() + "' with no live event (ledger v" + r.version() + ")");
                    }
                    String newKey = r.newKey();
                    BigDecimal newValue = r.newValue();
                    if (!newKey.equals(e.key())) {
                        out.computeIfAbsent(newKey, k -> new Aggregate()).add(newValue);
                        Aggregate old = out.get(e.key());
                        old.remove(e.value());
                        old.assertNoNegativeDrift();
                        if (old.count() == 0 && old.sum().signum() == 0) {
                            out.remove(e.key());
                        }
                    } else if (newValue.compareTo(e.value()) != 0) {
                        Aggregate agg = out.get(e.key());
                        agg.remove(e.value());
                        agg.add(newValue);
                    }
                    live.put(r.eventId(), new ReEvent(newKey, newValue));
                }
            }
        }
        return out;
    }

    private record ReEvent(String key, BigDecimal value) {
    }

    /**
     * Cross-checks the live streaming aggregates against a fresh ledger replay.
     *
     * @return list of discrepancies (empty when streaming state equals the oracle)
     */
    public synchronized List<String> verifyAgainstLedger() {
        Map<String, Aggregate> reference = recomputeFromLedger(ledger);
        List<String> diffs = new ArrayList<>();

        Set<String> keys = new java.util.HashSet<>();
        keys.addAll(aggregates.keySet());
        keys.addAll(reference.keySet());
        for (String key : keys) {
            Aggregate live = aggregates.get(key);
            Aggregate ref = reference.get(key);
            BigDecimal liveSum = live == null ? BigDecimal.ZERO : live.sum();
            long liveCount = live == null ? 0 : live.count();
            BigDecimal refSum = ref == null ? BigDecimal.ZERO : ref.sum();
            long refCount = ref == null ? 0 : ref.count();
            if (liveSum.compareTo(refSum) != 0 || liveCount != refCount) {
                diffs.add("key '" + key + "': streaming=(sum=" + liveSum + ",count=" + liveCount
                        + ") reference=(sum=" + refSum + ",count=" + refCount + ")");
            }
        }
        return diffs;
    }

    // ---------------------------------------------------------------------
    // Per-event chain state
    // ---------------------------------------------------------------------

    private static final class IdState {
        Event current;
        long nextVersion = 1;
        final TreeMap<Long, IngestOp> pendingVersioned = new TreeMap<>();
        final List<IngestOp> pendingUnversioned = new ArrayList<>();
        /** True after a terminal RETRACT fully consumed a chain. */
        boolean consumed;
    }
}
