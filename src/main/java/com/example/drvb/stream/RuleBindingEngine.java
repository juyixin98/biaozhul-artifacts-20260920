package com.example.drvb.stream;

import com.example.drvb.core.Event;
import com.example.drvb.core.RuleRegistry;
import com.example.drvb.core.RuleRegistryException;
import com.example.drvb.core.RuleVersion;
import com.example.drvb.time.Scheduler;
import com.example.drvb.time.TimeSource;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Exact reference stream-processing engine for dynamic rule version binding.
 *
 * <h2>Binding rules (all decisions are by event time)</h2>
 * <ol>
 *   <li>Before the first rule version is bootstrapped, every event is rejected
 *       with {@code MISSING_VERSION} — the engine <b>never</b> silently applies
 *       a later/newer rule set to an event whose time lacks a version.</li>
 *   <li>Otherwise the version in force at the event time is selected; a late
 *       event therefore runs against the historical version, not the latest.</li>
 *   <li>If the needed version was garbage collected, the event is rejected
 *       with {@code VERSION_RECLAIMED}.</li>
 *   <li>Events beyond the lateness horizon
 *       ({@code eventTime < watermark - allowedLateness}) are rejected with
 *       {@code TOO_LATE}; on-time and merely-late events are processed.</li>
 * </ol>
 *
 * <h2>Hot updates</h2>
 * Publication appends an immutable version whose {@code effectiveFrom} is
 * strictly after the previous version's; versions may be registered in
 * advance, and no update can change the rule content bound to an already
 * closed interval. Rollback appends a new version copying an earlier one's
 * rules (new id, identical checksum).
 *
 * <h2>Determinism</h2>
 * The engine is single-thread-safe via a single monitor; processing time is
 * read only through the injected {@link TimeSource}; maintenance runs via the
 * injected {@link Scheduler}. With {@code SimClock} + {@code ManualScheduler}
 * every run is exactly reproducible.
 */
public final class RuleBindingEngine {

    /** Rejection reason codes surfaced verbatim in JSON responses. */
    public static final String MISSING_VERSION = "MISSING_VERSION";
    public static final String VERSION_RECLAIMED = "VERSION_RECLAIMED";
    public static final String TOO_LATE = "TOO_LATE";

    private final RuleRegistry registry;
    private final WatermarkTracker watermarks;
    private final ResultStore store;
    private final TimeSource clock;

    private final long allowedLatenessMillis;
    private final long resultRetentionMillis;

    private long rejectedMissing;
    private long rejectedReclaimed;
    private long rejectedTooLate;
    private long acceptedCount;
    private int lastReclaimedCount;

    public RuleBindingEngine(RuleRegistry registry,
                             WatermarkTracker watermarks,
                             ResultStore store,
                             TimeSource clock,
                             long allowedLatenessMillis,
                             long resultRetentionMillis) {
        this.registry = registry;
        this.watermarks = watermarks;
        this.store = store;
        this.clock = clock;
        if (allowedLatenessMillis < 0) {
            throw new IllegalArgumentException("allowedLatenessMillis >= 0");
        }
        if (resultRetentionMillis < 0) {
            throw new IllegalArgumentException("resultRetentionMillis >= 0");
        }
        this.allowedLatenessMillis = allowedLatenessMillis;
        this.resultRetentionMillis = resultRetentionMillis;
    }

    // ------------------------------------------------------------ rule APIs

    public RuleVersion bootstrap(RuleVersion v) {
        RuleVersion installed = registry.bootstrap(v);
        registry.noteWatermark(watermarks.watermark());
        return installed;
    }

    public RuleVersion publish(RuleVersion v) {
        return registry.publish(v);
    }

    public RuleVersion rollback(String sourceVersionId, String newVersionId,
                                long effectiveFrom, String description) {
        return registry.rollback(sourceVersionId, newVersionId, effectiveFrom,
                clock.nowMillis(), description);
    }

    // -------------------------------------------------------------- ingest

    /** Feeds one event. Rejected events are recorded with their reason. */
    public IngestResult ingest(Event event) {
        synchronized (this) {
            long processedAt = clock.nowMillis();
            long wmBefore = watermarks.watermark();
            long horizon = RuleRegistry.saturatingSubtract(wmBefore, allowedLatenessMillis);

            String reason = null;
            RuleVersion selected = null;

            // 1) Version binding is decided first, purely by event time against
            //    the immutable timeline. Missing or reclaimed versions are
            //    never papered over with the latest rule set.
            RuleRegistry.Resolution res = registry.resolve(event.eventTime());
            if (res instanceof RuleRegistry.Missing) {
                reason = MISSING_VERSION;
            } else if (res instanceof RuleRegistry.Reclaimed) {
                // The version interval predates the oldest retained version.
                reason = VERSION_RECLAIMED;
            } else if (res instanceof RuleRegistry.Bound b) {
                // 2) A binding exists; reject only if the event arrives beyond
                //    the lateness window (the version interval is still kept,
                //    but the system no longer accepts events this old).
                if (wmBefore != Long.MIN_VALUE && event.eventTime() < horizon) {
                    reason = TOO_LATE;
                } else {
                    selected = b.version();
                }
            }

            // Observation order: first decide binding against the watermark as
            // it stood when the event arrived, then advance the watermark —
            // exactly like a streaming operator (assign then advance).
            watermarks.observe(event.eventTime());
            registry.noteWatermark(watermarks.watermark());

            if (reason != null) {
                IngestResult rejected = IngestResult.rejected(event, reason,
                        watermarks.watermark(), processedAt);
                store.appendRejected(rejected);
                switch (reason) {
                    case MISSING_VERSION -> rejectedMissing++;
                    case VERSION_RECLAIMED -> rejectedReclaimed++;
                    case TOO_LATE -> rejectedTooLate++;
                    default -> { }
                }
                return rejected;
            }

            boolean late = wmBefore != Long.MIN_VALUE && event.eventTime() < wmBefore;
            List<IngestResult.RuleMatch> matches =
                    IngestResult.evaluate(selected, event);
            IngestResult accepted = IngestResult.accepted(event, selected, matches,
                    late, watermarks.watermark(), processedAt);
            store.appendAccepted(accepted);
            acceptedCount++;
            return accepted;
        }
    }

    /** Feeds events in the given order (caller controls the interleaving). */
    public List<IngestResult> ingestAll(Iterable<Event> events) {
        List<IngestResult> out = new ArrayList<>();
        for (Event e : events) {
            out.add(ingest(e));
        }
        return out;
    }

    // ------------------------------------------------------- administration

    /**
     * Runs one maintenance cycle: purges results older than the retention
     * window, then garbage-collects rule versions that satisfy <b>both</b>
     * preconditions:
     * <ol>
     *   <li>the successor version starts at or before
     *       {@code watermark - allowedLateness} (no late event can still land
     *       in the old version's interval), and</li>
     *   <li>no retained result references the version (auditability).</li>
     * </ol>
     */
    public synchronized GcReport runMaintenance() {
        long now = clock.nowMillis();
        long cutoff = RuleRegistry.saturatingSubtract(now, resultRetentionMillis);
        int purged = store.purgeProcessedBefore(cutoff);

        long wm = watermarks.watermark();
        List<RuleVersion> removed = registry.reclaim(wm, allowedLatenessMillis,
                store.referencedVersionIds());
        this.lastReclaimedCount = removed.size();
        return new GcReport(now, wm, purged, removed.size(), removed,
                registry.versions());
    }

    /** GC without mutating state; shows which versions are eligible and why. */
    public synchronized GcPreview gcPreview() {
        long wm = watermarks.watermark();
        var referenced = store.referencedVersionIds();
        var eligible = registry.reclaimPreview(wm, allowedLatenessMillis, referenced);
        return new GcPreview(wm,
                RuleRegistry.saturatingSubtract(wm, allowedLatenessMillis),
                eligible, referenced, registry.versions());
    }

    /** Forces the watermark forward (admin / idle timer). Never backwards. */
    public synchronized long advanceWatermark(long newWatermark) {
        long w = watermarks.advanceTo(newWatermark);
        registry.noteWatermark(w);
        return w;
    }

    public synchronized Map<String, Object> stats() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("watermark", watermarks.watermark());
        m.put("maxEventTime", watermarks.maxEventTime());
        m.put("allowedLatenessMillis", allowedLatenessMillis);
        m.put("watermarkDelayMillis", watermarks.delayMillis());
        m.put("resultRetentionMillis", resultRetentionMillis);
        m.put("accepted", acceptedCount);
        m.put("rejected", Map.of(
                "total", rejectedMissing + rejectedReclaimed + rejectedTooLate,
                MISSING_VERSION, rejectedMissing,
                VERSION_RECLAIMED, rejectedReclaimed,
                TOO_LATE, rejectedTooLate));
        m.put("retainedVersionCount", registry.versions().size());
        m.put("retainedAcceptedResults", store.acceptedCount());
        m.put("lastReclaimedCount", lastReclaimedCount);
        return m;
    }

    public RuleRegistry registry() {
        return registry;
    }

    public ResultStore store() {
        return store;
    }

    public long allowedLatenessMillis() {
        return allowedLatenessMillis;
    }

    // ------------------------------------------------------------- reports

    /** Result of a maintenance cycle. */
    public record GcReport(long atMillis, long watermark, int resultsPurged,
                          int versionsReclaimed, List<RuleVersion> removed,
                          List<RuleVersion> retainedVersions) {
    }

    /** Non-mutating view of GC eligibility. */
    public record GcPreview(long watermark, long horizon,
                           List<RuleVersion> eligibleForReclaim,
                           java.util.Set<String> referencedVersionIds,
                           List<RuleVersion> retainedVersions) {
    }
}
