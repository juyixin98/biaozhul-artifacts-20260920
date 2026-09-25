package streamagg.server;

import streamagg.engine.StreamCorrectionEngine;
import streamagg.json.Json;
import streamagg.model.Aggregate;
import streamagg.model.Event;
import streamagg.model.IngestOp;
import streamagg.model.IngestResult;
import streamagg.model.OutputSnapshot;
import streamagg.model.ResolvedOp;
import streamagg.time.Clock;
import streamagg.time.ScheduledTask;
import streamagg.time.Scheduler;

import java.time.Duration;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Application service around the engine: single/batch ingest, queries, exact
 * reference verification and what-if replay of a raw operation sequence through
 * a fresh engine.
 */
public final class AggregateService {

    private final StreamCorrectionEngine engine;
    private ScheduledTask periodicTask;

    public AggregateService(StreamCorrectionEngine engine) {
        this.engine = engine;
    }

    public static AggregateService create(Clock clock) {
        return new AggregateService(StreamCorrectionEngine.builder().clock(clock).build());
    }

    public StreamCorrectionEngine engine() {
        return engine;
    }

    // ------------------------------------------------------------------
    // Ingest
    // ------------------------------------------------------------------

    public IngestResult ingestOne(Json.JsonValue body) {
        IngestOp op = Codec.toIngestOp(body);
        return engine.ingest(op);
    }

    public record BatchItemResult(int index, IngestOp op, IngestResult result) {
    }

    public List<BatchItemResult> ingestBatch(Json.JsonValue body) {
        Json.JsonValue itemsNode = body.get("operations");
        if (itemsNode == null || !itemsNode.isArray()) {
            throw new IllegalArgumentException("'operations' array is required");
        }
        List<BatchItemResult> out = new ArrayList<>();
        int index = 0;
        for (Json.JsonValue item : itemsNode.asArray()) {
            IngestOp op = Codec.toIngestOp(item);
            IngestResult r = engine.ingest(op);
            out.add(new BatchItemResult(index++, op, r));
        }
        return out;
    }

    // ------------------------------------------------------------------
    // Queries
    // ------------------------------------------------------------------

    public List<Map<String, Object>> aggregateViews() {
        List<Map<String, Object>> out = new ArrayList<>();
        for (Map.Entry<String, Aggregate> e : engine.aggregates().entrySet()) {
            out.add(Codec.aggregateJson(e.getKey(), e.getValue()));
        }
        return out;
    }

    public Map<String, Object> aggregateResponse(boolean includeVerify) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("aggregates", aggregateViews());
        m.put("liveEvents", engine.events().size());
        m.put("pendingOps", engine.totalPending());
        m.put("watermark", engine.watermark());
        if (includeVerify) {
            m.put("verification", verificationView());
        }
        return m;
    }

    public List<Map<String, Object>> eventViews() {
        List<Map<String, Object>> out = new ArrayList<>();
        for (Event e : engine.events().values()) {
            out.add(Codec.eventJson(e));
        }
        return out;
    }

    public List<Map<String, Object>> pendingViews() {
        List<Map<String, Object>> out = new ArrayList<>();
        for (String eventId : engine.eventIdsWithPending()) {
            addPending(out, eventId);
        }
        return out;
    }

    private void addPending(List<Map<String, Object>> out, String eventId) {
        List<IngestOp> pending = engine.pendingFor(eventId);
        if (!pending.isEmpty()) {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("eventId", eventId);
            List<Object> ops = new ArrayList<>();
            for (IngestOp p : pending) {
                ops.add(Codec.ingestOpJson(p));
            }
            m.put("pending", ops);
            out.add(m);
        }
    }

    public List<Map<String, Object>> ledgerViews(Integer limit) {
        List<ResolvedOp> ledger = engine.ledger();
        if (limit != null && limit >= 0 && ledger.size() > limit) {
            ledger = ledger.subList(ledger.size() - limit, ledger.size());
        }
        List<Map<String, Object>> out = new ArrayList<>();
        for (ResolvedOp r : ledger) {
            out.add(Codec.resolvedOpJson(r));
        }
        return out;
    }

    // ------------------------------------------------------------------
    // Reference implementation / verification
    // ------------------------------------------------------------------

    public Map<String, Object> verificationView() {
        List<String> diffs = engine.verifyAgainstLedger();
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("matchesLedgerRecomputation", diffs.isEmpty());
        m.put("discrepancies", diffs);
        m.put("ledgerEntries", engine.ledger().size());
        return m;
    }

    /**
     * Exact small-data reference check: replays the supplied raw operation list
     * through a fresh engine (exercising buffering, idempotency and draining) and
     * then verifies the fresh engine's live aggregates equal its own ledger replay.
     *
     * <p>When no body operations are supplied, the current engine's resolved
     * ledger is replayed from scratch instead.
     */
    public Map<String, Object> replayCheck(Json.JsonValue bodyOrNull, Clock replayClock) {
        StreamCorrectionEngine fresh = StreamCorrectionEngine.builder().clock(replayClock).build();

        int rawCount = 0;
        List<Map<String, Object>> statuses = new ArrayList<>();

        if (bodyOrNull != null && bodyOrNull.get("operations") != null) {
            Json.JsonValue items = bodyOrNull.get("operations");
            rawCount = items.asArray().size();
            int idx = 0;
            for (Json.JsonValue item : items.asArray()) {
                IngestOp op = Codec.toIngestOp(item);
                IngestResult r = fresh.ingest(op);
                Map<String, Object> row = new LinkedHashMap<>();
                row.put("index", idx++);
                row.put("status", r.status().name());
                row.put("message", r.message());
                if (!r.drained().isEmpty()) {
                    List<Object> drained = new ArrayList<>();
                    for (ResolvedOp d : r.drained()) {
                        drained.add(d.eventId() + "#v" + d.version() + ":" + d.op().name());
                    }
                    row.put("drained", drained);
                }
                statuses.add(row);
            }
        } else {
            // No raw operations supplied: recompute the current resolved ledger
            // independently with the exact small-data reference implementation.
            Map<String, Aggregate> reference = StreamCorrectionEngine.recomputeFromLedger(engine.ledger());
            List<String> diffs = new ArrayList<>();
            for (Map.Entry<String, Aggregate> x : reference.entrySet()) {
                Aggregate live = engine.aggregates().get(x.getKey());
                if (live == null || live.sum().compareTo(x.getValue().sum()) != 0
                        || live.count() != x.getValue().count()) {
                    diffs.add("key '" + x.getKey() + "': live=" + live
                            + " reference=(sum=" + x.getValue().sum() + ",count=" + x.getValue().count() + ")");
                }
            }
            for (String k : engine.aggregates().keySet()) {
                if (!reference.containsKey(k)) {
                    diffs.add("key '" + k + "': present live but absent in reference");
                }
            }
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("inputOperations", 0);
            m.put("resolvedLedgerEntries", engine.ledger().size());
            m.put("matchesLedgerRecomputation", diffs.isEmpty());
            m.put("discrepancies", diffs);
            m.put("aggregatesAfterReplay", aggregateViewsOfMap(reference));
            m.put("results", List.of());
            m.put("warnings", engine.warnings());
            return m;
        }
        List<String> diffs = fresh.verifyAgainstLedger();
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("inputOperations", rawCount);
        m.put("resolvedLedgerEntries", fresh.ledger().size());
        m.put("pendingAfterReplay", fresh.totalPending());
        m.put("matchesLedgerRecomputation", diffs.isEmpty());
        m.put("discrepancies", diffs);
        m.put("aggregatesAfterReplay", aggregateViewsOf(fresh));
        m.put("results", statuses);
        m.put("warnings", fresh.warnings());
        return m;
    }

    private List<Map<String, Object>> aggregateViewsOf(StreamCorrectionEngine e) {
        List<Map<String, Object>> out = new ArrayList<>();
        for (Map.Entry<String, Aggregate> x : e.aggregates().entrySet()) {
            out.add(Codec.aggregateJson(x.getKey(), x.getValue()));
        }
        return out;
    }

    private List<Map<String, Object>> aggregateViewsOfMap(Map<String, Aggregate> aggs) {
        List<Map<String, Object>> out = new ArrayList<>();
        for (Map.Entry<String, Aggregate> x : aggs.entrySet()) {
            out.add(Codec.aggregateJson(x.getKey(), x.getValue()));
        }
        return out;
    }

    // ------------------------------------------------------------------
    // Scheduling / reset
    // ------------------------------------------------------------------

    /** Starts periodic output emission. Returns the listener-collected snapshots count support. */
    public synchronized void startPeriodicEmission(Scheduler scheduler, Duration period,
                                                   java.util.function.Consumer<OutputSnapshot> sink) {
        stopPeriodicEmission();
        engine.addListener(sink::accept);
        periodicTask = scheduler.scheduleAtFixedRate(period, period,
                () -> {
                    OutputSnapshot snap = engine.emitSnapshot();
                    sink.accept(snap);
                });
    }

    public synchronized void stopPeriodicEmission() {
        if (periodicTask != null) {
            periodicTask.cancel();
            periodicTask = null;
        }
    }

    public void reset() {
        engine.reset();
    }
}
