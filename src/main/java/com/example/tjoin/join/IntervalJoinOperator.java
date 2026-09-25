package com.example.tjoin.join;

import com.example.tjoin.model.AcceptStatus;
import com.example.tjoin.model.BufferOverflowPolicy;
import com.example.tjoin.model.JoinConfig;
import com.example.tjoin.model.JoinMetrics;
import com.example.tjoin.model.JoinResult;
import com.example.tjoin.model.ProcessResult;
import com.example.tjoin.model.SideName;
import com.example.tjoin.model.StreamEvent;
import com.example.tjoin.state.SideState;
import com.example.tjoin.time.Clock;
import com.example.tjoin.time.Scheduler;
import com.example.tjoin.watermark.WatermarkGenerator;

import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Set;

/**
 * Event-time interval join over two independent streams.
 *
 * <p>For left event {@code (k, tL)} and right event {@code (k, tR)} the pair
 * is emitted iff {@code lowerBound <= tR - tL <= upperBound}. A pair is
 * emitted exactly once: when the second of its two events arrives (the
 * earlier event is found by key + time-range scan in the opposite state).
 * Redelivery of an already-seen event id is ignored, and a global
 * emitted-pair set additionally guards against any double emission.</p>
 *
 * <h2>Independent watermarks and state cleanup</h2>
 * Each side carries its own bounded-out-of-orderness watermark. Buffered
 * records are removed only once they can no longer match <em>any</em>
 * future (non-late) event from the other side:
 * <pre>
 *     left  cleanup when  tL &lt; watermarkRight - upperBound
 *     right cleanup when  tR &lt; watermarkLeft  + lowerBound
 * </pre>
 * A side marked idle <strong>freezes</strong> its watermark at the last
 * observed value: it neither advances (so a stalled side cannot push
 * cleanup past records a recovering event could still match) nor is
 * withdrawn to -inf (so genuinely stale records are still reaped and
 * buffers stay bounded). When the side recovers, its watermark resumes
 * advancing and normal cleanup continues.
 *
 * <h2>Late events</h2>
 * An event with {@code eventTime < own-side watermark} is dropped (counted
 * in metrics). The check is inclusive of the watermark: events exactly at
 * the watermark are still kept.
 *
 * <h2>Buffer bound</h2>
 * Each side has an optional hard cap. With {@link BufferOverflowPolicy#REJECT}
 * the over-cap event is refused and nothing buffered is removed; with
 * {@link BufferOverflowPolicy#DROP_OLDEST} the globally oldest event on that
 * side is evicted (counted) to make room.
 *
 * <p>Time is injected: {@link Clock} stamps emissions and tracks activity;
 * {@link Scheduler} drives periodic idleness checks. No threads or wall
 * clock are used internally.</p>
 */
public final class IntervalJoinOperator {

    private static final long IDLE_TICK_PERIOD_MILLIS = 100L;

    private final JoinConfig config;
    private final Clock clock;
    private final JoinMetrics metrics = new JoinMetrics();

    private final SideState leftState = new SideState();
    private final SideState rightState = new SideState();

    private final WatermarkGenerator leftWatermark;
    private final WatermarkGenerator rightWatermark;

    /** Emitted pairs keyed as "leftId| rightId". */
    private final Set<String> emittedPairs = new HashSet<>();

    /**
     * Ids ever admitted on each side, retained for the job's lifetime. An
     * id removed from buffered state by cleanup is still recognized on
     * redelivery and ignored (event ids are not reused).
     */
    private final Set<String> seenLeftIds = new HashSet<>();
    private final Set<String> seenRightIds = new HashSet<>();

    public IntervalJoinOperator(JoinConfig config, Clock clock, Scheduler scheduler) {
        this.config = config;
        this.clock = clock;
        this.leftWatermark = new WatermarkGenerator("left",
                config.left().maxOutOfOrderness(), config.left().idleTimeoutMillis());
        this.rightWatermark = new WatermarkGenerator("right",
                config.right().maxOutOfOrderness(), config.right().idleTimeoutMillis());

        leftWatermark.setIdleListener((idle, at) -> {
            metrics.leftIdle = idle;
            // On recovery the advancing watermark immediately prunes
            // everything that has become unmatchable.
            if (!idle) {
                cleanup();
            }
        });
        rightWatermark.setIdleListener((idle, at) -> {
            metrics.rightIdle = idle;
            if (!idle) {
                cleanup();
            }
        });

        if (scheduler != null) {
            long tick = IDLE_TICK_PERIOD_MILLIS;
            scheduler.scheduleAfter(tick, new Runnable() {
                @Override
                public void run() {
                    onProcessingTimeTick();
                    scheduler.scheduleAfter(tick, this);
                }
            });
        }
    }

    // ------------------------------------------------------------------
    // Public API
    // ------------------------------------------------------------------

    /**
     * Feed a data event into one side. Synchronized: the service may call a
     * job concurrently from multiple HTTP worker threads, and operator
     * state is not otherwise thread-safe.
     */
    public synchronized ProcessResult processEvent(SideName side, StreamEvent event) {
        long processingTime = clock.currentTimeMillis();
        if (side == SideName.LEFT) {
            return process(side, event, leftState, rightState, leftWatermark,
                    processingTime, true);
        }
        return process(side, event, rightState, leftState, rightWatermark,
                processingTime, false);
    }

    /**
     * Inject an explicit watermark for one side (service/test API). Advances
     * the side watermark and runs state cleanup.
     */
    public synchronized int advanceWatermark(SideName side, long watermark) {
        long processingTime = clock.currentTimeMillis();
        if (side == SideName.LEFT) {
            leftWatermark.onWatermark(watermark, processingTime);
            metrics.leftWatermark = leftWatermark.currentWatermark();
        } else {
            rightWatermark.onWatermark(watermark, processingTime);
            metrics.rightWatermark = rightWatermark.currentWatermark();
        }
        return cleanup();
    }

    /** Processing-time tick (also invoked by the internal scheduled task). */
    public synchronized void onProcessingTimeTick() {
        long now = clock.currentTimeMillis();
        leftWatermark.maybeCheckIdle(now);
        rightWatermark.maybeCheckIdle(now);
    }

    public synchronized JoinMetrics metrics() {
        return metrics;
    }

    public synchronized long leftWatermark() {
        return leftWatermark.currentWatermark();
    }

    public synchronized long rightWatermark() {
        return rightWatermark.currentWatermark();
    }

    public synchronized int leftBufferSize() {
        return leftState.size();
    }

    public synchronized int rightBufferSize() {
        return rightState.size();
    }

    // ------------------------------------------------------------------
    // Core processing
    // ------------------------------------------------------------------

    private ProcessResult process(SideName ownSide,
                                  StreamEvent incoming,
                                  SideState ownState,
                                  SideState oppositeState,
                                  WatermarkGenerator ownWmGen,
                                  long processingTime,
                                  boolean incomingIsLeft) {
        // 1. Duplicate delivery suppression. Ids are remembered for the
        //    whole job lifetime (not just while buffered), so an event
        //    redelivered after watermark cleanup is still ignored.
        Set<String> seenIds = ownSide == SideName.LEFT ? seenLeftIds : seenRightIds;
        if (!seenIds.add(incoming.getId())) {
            metrics.duplicatesDropped.incrementAndGet();
            return ProcessResult.rejected(AcceptStatus.DUPLICATE);
        }

        // 2. Late-event check against THIS side's own watermark.
        long ownWmBefore = ownWmGen.currentWatermark();
        if (ownWmBefore != Long.MIN_VALUE && incoming.getTimestamp() < ownWmBefore) {
            seenIds.remove(incoming.getId()); // rejected: id may be reused by a valid retry
            metrics.lateDropped.incrementAndGet();
            return ProcessResult.rejected(AcceptStatus.LATE);
        }

        // 3. Buffer bound handling.
        StreamEvent evicted = null;
        int cap = sideConfig(ownSide).maxBufferSize();
        if (cap > 0 && ownState.size() >= cap) {
            if (sideConfig(ownSide).overflowPolicy() == BufferOverflowPolicy.REJECT) {
                seenIds.remove(incoming.getId()); // rejected: id may be reused by a valid retry
                metrics.bufferRejected.incrementAndGet();
                return ProcessResult.rejected(AcceptStatus.BUFFER_FULL);
            }
            evicted = ownState.removeOldest();
            metrics.oldestEvicted.incrementAndGet();
        }

        // 4. Advance this side's watermark (may enable cleanup of BOTH states).
        ownWmGen.onEvent(incoming.getTimestamp(), processingTime);
        metrics.leftWatermark = leftWatermark.currentWatermark();
        metrics.rightWatermark = rightWatermark.currentWatermark();

        // 5. Match against the opposite buffered state BEFORE buffering the
        //    new event (the new event is not in the scanned state anyway).
        List<JoinResult> emitted = new ArrayList<>();
        long t = incoming.getTimestamp();
        List<StreamEvent> candidates;
        if (incomingIsLeft) {
            // tL = t; need lowerBound <= tR - t <= upperBound
            candidates = oppositeState.rangeByKey(incoming.getKey(),
                    t + config.lowerBound(), t + config.upperBound());
            for (StreamEvent right : candidates) {
                emitPair(incoming, right, processingTime, emitted);
            }
        } else {
            // tR = t; need lowerBound <= t - tL <= upperBound
            candidates = oppositeState.rangeByKey(incoming.getKey(),
                    t - config.upperBound(), t - config.lowerBound());
            for (StreamEvent left : candidates) {
                emitPair(left, incoming, processingTime, emitted);
            }
        }

        // 6. Buffer the new event for future arrivals on the other side.
        ownState.add(incoming);
        if (ownSide == SideName.LEFT) {
            metrics.leftEventsAccepted.incrementAndGet();
        } else {
            metrics.rightEventsAccepted.incrementAndGet();
        }
        metrics.leftBuffered = leftState.size();
        metrics.rightBuffered = rightState.size();

        // 7. Watermark-driven cleanup of records that can no longer match.
        int cleaned = cleanup();
        return ProcessResult.of(AcceptStatus.ACCEPTED, emitted, evicted, cleaned);
    }

    private void emitPair(StreamEvent left, StreamEvent right, long processingTime,
                          List<JoinResult> sink) {
        String pairKey = pairKey(left.getId(), right.getId());
        if (!emittedPairs.add(pairKey)) {
            metrics.pairsSuppressed.incrementAndGet();
            return;
        }
        sink.add(new JoinResult(left, right, processingTime));
        metrics.pairsEmitted.incrementAndGet();
    }

    private static String pairKey(String leftId, String rightId) {
        return leftId + "|" + rightId;
    }

    /**
     * Remove records that can no longer match any future non-late event.
     *
     * <p>An idle side's watermark is <strong>frozen</strong> at its last
     * observed value (not withdrawn to MIN_VALUE and not advanced): while
     * stalled it emits nothing new, and on recovery no valid event can
     * carry a time behind that frozen watermark (such an event would be
     * late and dropped). Freezing therefore keeps every still-matchable
     * record buffered while allowing stale records to be reaped —
     * staleness in processing time alone never discards anything.</p>
     *
     * @return total number of records removed from both states
     */
    private int cleanup() {
        int removed = 0;

        long effRightWm = rightWatermark.currentWatermark();
        if (effRightWm != Long.MIN_VALUE) {
            // Left tL can only match future right events tR >= effRightWm
            // (right wm frozen while idle) when
            // tR - tL <= upperBound  <=>  tL >= effRightWm - upperBound.
            long leftThreshold = effRightWm - config.upperBound();
            List<StreamEvent> gone = leftState.removeOlderThan(leftThreshold);
            if (!gone.isEmpty()) {
                metrics.leftStateCleaned.addAndGet(gone.size());
                removed += gone.size();
            }
        }

        long effLeftWm = leftWatermark.currentWatermark();
        if (effLeftWm != Long.MIN_VALUE) {
            // Right tR can only match future left events tL >= effLeftWm
            // (left wm frozen while idle) when
            // tR - tL >= lowerBound  <=>  tR >= effLeftWm + lowerBound.
            long rightThreshold = effLeftWm + config.lowerBound();
            List<StreamEvent> gone = rightState.removeOlderThan(rightThreshold);
            if (!gone.isEmpty()) {
                metrics.rightStateCleaned.addAndGet(gone.size());
                removed += gone.size();
            }
        }

        metrics.leftBuffered = leftState.size();
        metrics.rightBuffered = rightState.size();
        return removed;
    }

    private JoinConfig.SideConfig sideConfig(SideName side) {
        return side == SideName.LEFT ? config.left() : config.right();
    }
}
