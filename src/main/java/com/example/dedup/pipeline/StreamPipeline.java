package com.example.dedup.pipeline;

import com.example.dedup.dedup.DedupState;
import com.example.dedup.dedup.PayloadHasher;
import com.example.dedup.json.Json;
import com.example.dedup.model.Event;
import com.example.dedup.model.IngestResult;
import com.example.dedup.model.LateSideOutput;
import com.example.dedup.model.Metrics;
import com.example.dedup.model.WindowResult;
import com.example.dedup.time.Clock;
import com.example.dedup.time.Scheduler;
import com.example.dedup.tombstone.TombstoneDecision;
import com.example.dedup.tombstone.TombstoneStore;
import com.example.dedup.watermark.BoundedOutOfOrdernessWatermarks;
import com.example.dedup.watermark.WatermarkGenerator;
import com.example.dedup.window.TumblingWindowOperator;

import java.util.ArrayList;
import java.util.List;

/**
 * The full, deterministic event-processing pipeline.
 *
 * Per event:
 *   1. dedup lookup against bounded state (pre-watermark-update)
 *   2. for first occurrences: tombstone check (UPSERT) / tombstone record (DELETE)
 *   3. watermark observation (forwarded events only)
 *   4. window fold or late-drop side output
 *   5. watermark progress -> window firing, then dedup/tombstone GC
 *
 * All state is snapshot-able for restart recovery. Timing is fully
 * injectable via {@link Clock} + {@link Scheduler}; nothing here reads the
 * system clock directly, so processing-time clock rollback can never cause
 * premature state release (release is driven solely by event time).
 */
public final class StreamPipeline {

    private final PipelineConfig config;
    private final DedupState dedup;
    private final TombstoneStore tombstones;
    private final TumblingWindowOperator windows;
    private final WatermarkGenerator watermarks;
    private final Metrics metrics;

    private final List<WindowResult> windowOutputs = new ArrayList<>();
    private final List<LateSideOutput> lateOutputs = new ArrayList<>();

    public StreamPipeline(PipelineConfig config, Clock clock) {
        this(config, clock, null);
    }

    public StreamPipeline(PipelineConfig config, Clock clock, Scheduler scheduler) {
        // Invariant: dedup/tombstone state must live at least as long as a
        // window can remain open (window size + lateness). Otherwise a
        // duplicate could arrive after its dedup state was released yet
        // still be foldable into an open window, which could double-count
        // window output. With this invariant, every unverifiable occurrence
        // necessarily belongs to an already-fired window and is side-output.
        long windowLifetime = config.windowSizeMillis() + config.windowLatenessMillis();
        if (config.retentionMillis() < windowLifetime) {
            throw new IllegalArgumentException(
                    "retentionMillis (" + config.retentionMillis()
                            + ") must be >= windowSizeMillis + windowLatenessMillis ("
                            + windowLifetime + ") to keep window output duplicate-free");
        }
        if (config.tombstoneTtlMillis() < windowLifetime) {
            throw new IllegalArgumentException(
                    "tombstoneTtlMillis must be >= window size + lateness");
        }
        this.config = config;
        this.dedup = new DedupState(config.maxEntries(), config.retentionMillis());
        this.tombstones = new TombstoneStore(config.maxTombstoneKeys(), config.tombstoneTtlMillis());
        this.windows = new TumblingWindowOperator(config.windowSizeMillis(), config.windowLatenessMillis());
        this.watermarks = new BoundedOutOfOrdernessWatermarks(config.outOfOrdernessMillis());
        this.metrics = new Metrics();

        if (scheduler != null && config.autoWatermarkPeriodMillis() > 0) {
            long period = config.autoWatermarkPeriodMillis();
            // Periodic emission: try to raise the watermark to the observed
            // heuristic. The target derives from event time, never from the
            // wall clock, so a wall-clock rollback cannot release state.
            scheduler.schedulePeriodic(period, this::onAutoTick);
        }
    }

    private synchronized void onAutoTick() {
        long target = watermarks.heuristicWatermark();
        long before = watermarks.watermark();
        if (target > before && watermarks.tryAdvance(target)) {
            fireAndGc(before, watermarks.watermark());
            publishGauges();
        }
    }

    public Metrics metrics() {
        return metrics;
    }

    public long watermark() {
        return watermarks.watermark();
    }

    public PipelineConfig config() {
        return config;
    }

    public synchronized IngestResult ingest(Event e) {
        metrics.eventsIn++;
        long wmBefore = watermarks.watermark();
        boolean late = wmBefore != WatermarkGenerator.NO_WATERMARK && e.eventTime() < wmBefore;
        if (late) {
            metrics.lateIn++;
        }

        String payloadHash = PayloadHasher.hash(e.payload());
        var dr = dedup.process(e.id(), e.eventTime(), payloadHash, wmBefore);
        if (dr.capacityEvicted()) {
            metrics.capacityEvictions++;
        }

        IngestResult result;
        if (dr.duplicate()) {
            metrics.duplicatesIn++;
            if (dr.payloadMismatch()) {
                metrics.payloadMismatches++;
            }
            if (dr.eventTimeSkew()) {
                metrics.eventTimeSkews++;
            }
            result = new IngestResult(e, false, true, dr.payloadMismatch(), dr.eventTimeSkew(),
                    late, false, false, false, false, dr.capacityEvicted());
            // Duplicates don't advance event time, tombstones, or windows.
            return result;
        }

        if (dr.unverified()) {
            metrics.unverifiedDuplicates++;
        }

        // First live occurrence (within the promise window, or unverifiable).
        boolean suppressed = false;
        boolean tombUncertain = false;
        if (e.type() == Event.EventType.DELETE) {
            tombstones.addDelete(e.key(), e.eventTime());
        } else {
            TombstoneDecision td = tombstones.lookup(e.key(), e.eventTime(), wmBefore);
            if (td == TombstoneDecision.SUPPRESSED) {
                suppressed = true;
                metrics.tombstoneSuppressed++;
            } else if (td == TombstoneDecision.UNCERTAIN) {
                tombUncertain = true;
                metrics.tombstoneUncertain++;
            }
        }

        // Forwarded events observe event time (even suppressed ones, since
        // their event time genuinely belongs to the source stream).
        watermarks.observe(e.eventTime());

        boolean windowLate = false;
        if (!suppressed && windows.isWindowLate(e.eventTime(), watermarks.watermark())) {
            windowLate = true;
            metrics.windowLateDropped++;
            lateOutputs.add(new LateSideOutput(e, watermarks.watermark(), "window-already-fired"));
        } else if (!suppressed) {
            windows.add(e);
        }

        if (!suppressed && !windowLate) {
            metrics.acceptedOut++;
        }

        result = new IngestResult(e, true, false, false, false, late,
                dr.unverified(), suppressed, tombUncertain, windowLate, dr.capacityEvicted());

        advanceFromObserved(wmBefore);
        publishGauges();
        return result;
    }

    /** Explicitly advance the watermark (HTTP /watermark, or tests). */
    public synchronized boolean advanceWatermark(long target) {
        long before = watermarks.watermark();
        boolean ok = watermarks.tryAdvance(target);
        if (!ok) {
            metrics.watermarkRegressions++;
            return false;
        }
        fireAndGc(before, watermarks.watermark());
        publishGauges();
        return true;
    }

    private void advanceFromObserved(long oldWm) {
        long now = watermarks.watermark(); // heuristic recomputed on observe()
        if (now > oldWm) {
            fireAndGc(oldWm, now);
        }
    }

    private void fireAndGc(long oldWm, long newWm) {
        List<WindowResult> fired = windows.fire(newWm);
        if (!fired.isEmpty()) {
            windowOutputs.addAll(fired);
            metrics.windowsFired += fired.size();
        }
        long beforeDedup = dedup.size();
        metrics.watermarkEvictions += dedup.evictExpired(newWm);
        metrics.tombstoneEvictions += tombstones.evictExpired(newWm);
        metrics.watermark = newWm;
    }

    private void publishGauges() {
        metrics.watermark = watermarks.watermark();
        metrics.activeDedupEntries = dedup.size();
        metrics.activeTombstoneKeys = tombstones.sizeKeys();
    }

    /** Take and return fired window results (drains the output queue). */
    public synchronized List<WindowResult> drainWindowOutputs() {
        List<WindowResult> out = new ArrayList<>(windowOutputs);
        windowOutputs.clear();
        return out;
    }

    public synchronized List<LateSideOutput> drainLateOutputs() {
        List<LateSideOutput> out = new ArrayList<>(lateOutputs);
        lateOutputs.clear();
        return out;
    }

    // ----------------------------------------------------------------
    // Snapshot / restore (restart recovery)
    // ----------------------------------------------------------------

    public synchronized Json.Value snapshot() {
        Json.JsonObject root = Json.obj();
        root.members().put("version", Json.num(1));
        root.members().put("dedup", dedup.snapshot());
        root.members().put("tombstones", tombstones.snapshot());
        root.members().put("windows", windows.snapshot());
        Json.JsonObject wm = Json.obj();
        wm.members().put("watermark", Json.num(watermarks.watermark()));
        wm.members().put("maxObserved", Json.num(maxObserved()));
        root.members().put("watermarkState", wm);
        return root;
    }

    private long maxObserved() {
        return ((BoundedOutOfOrdernessWatermarks) watermarks).maxObserved();
    }

    public static StreamPipeline restore(Json.JsonObject root, PipelineConfig config,
                                         Clock clock, Scheduler scheduler) {
        StreamPipeline p = new StreamPipeline(config, clock, scheduler);
        if (root.get("dedup") instanceof Json.JsonObject d) {
            p.dedup.restore(d);
        }
        if (root.get("tombstones") instanceof Json.JsonObject t) {
            p.tombstones.restore(t);
        }
        if (root.get("windows") instanceof Json.JsonObject w) {
            p.windows.restore(w);
        }
        if (root.get("watermarkState") instanceof Json.JsonObject wm) {
            long w = wm.getLong("watermark", Long.MIN_VALUE);
            long m = wm.getLong("maxObserved", Long.MIN_VALUE);
            p.watermarks.restore(w, m);
            p.metrics.watermark = w;
        }
        p.publishGauges();
        return p;
    }
}
