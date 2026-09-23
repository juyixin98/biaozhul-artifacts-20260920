package com.example.watermark.windowing;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.function.Consumer;

import com.example.watermark.watermarks.LateEvent;
import com.example.watermark.watermarks.StreamEvent;

/**
 * Small-data exact reference implementation of event-time tumbling windows.
 *
 * <p>This is deliberately simple (buffers are in-memory lists) so its output
 * can be used as ground truth: for any input it shows exactly which events are
 * on time, which windows close at which watermark, and which events are late.
 *
 * <p>Window {@code [start, end)} with {@code start = floor(ts/size)*size} and
 * {@code end = start + size} closes when the watermark reaches {@code end}
 * ({@code watermark >= end}). Empty windows never fire.
 */
public final class TumblingWindowProcessor implements AutoCloseable {

    /** One open per-partition window. */
    private static final class Bucket {
        final long start;
        final long end;
        final List<Object> payloads = new ArrayList<>();

        Bucket(long start, long size) {
            this.start = start;
            this.end = start + size;
        }
    }

    private final long windowSizeMillis;
    private final Map<String, TreeMapShim> openWindowsByPartition = new LinkedHashMap<>();
    private final List<WindowResult> results = new ArrayList<>();
    private final CopyOnWriteArrayList<Consumer<WindowResult>> closeListeners =
            new CopyOnWriteArrayList<>();
    private final CopyOnWriteArrayList<Consumer<LateEvent>> droppedLateListeners =
            new CopyOnWriteArrayList<>();

    // Minimal sorted structure keyed by window start (we only iterate from the head).
    private static final class TreeMapShim {
        private final java.util.TreeMap<Long, Bucket> map = new java.util.TreeMap<>();

        Bucket bucketFor(long ts, long size) {
            long start = Math.floorDiv(ts, size) * size;
            return map.computeIfAbsent(start, s -> new Bucket(s, size));
        }

        java.util.TreeMap<Long, Bucket> raw() {
            return map;
        }
    }

    public TumblingWindowProcessor(long windowSizeMillis) {
        if (windowSizeMillis <= 0) {
            throw new IllegalArgumentException("window size must be positive: " + windowSizeMillis);
        }
        this.windowSizeMillis = windowSizeMillis;
    }

    public void addWindowCloseListener(Consumer<WindowResult> listener) {
        closeListeners.add(listener);
    }

    public void addDroppedLateListener(Consumer<LateEvent> listener) {
        droppedLateListeners.add(listener);
    }

    /** Apply an on-time event: buffers it into its partition/window. */
    public synchronized void onEvent(StreamEvent event) {
        TreeMapShim windows = openWindowsByPartition.computeIfAbsent(
                event.key(), k -> new TreeMapShim());
        Bucket bucket = windows.bucketFor(event.timestamp(), windowSizeMillis);
        bucket.payloads.add(event.payload());
    }

    /**
     * Apply a late event already detected by the watermark manager. The event
     * is never added to a window; if its window has already closed it is simply
     * recorded as dropped (and notified).
     */
    public synchronized void onLateEvent(LateEvent late) {
        StreamEvent event = late.event();
        long start = Math.floorDiv(event.timestamp(), windowSizeMillis) * windowSizeMillis;
        long end = start + windowSizeMillis;
        // Window closes at watermark >= end; the manager guarantees
        // timestamp <= globalWatermark, and end > timestamp, so the window may
        // or may not be closed depending on the watermark. Report accurately.
        boolean windowAlreadyClosed = late.globalWatermark() >= end;
        if (windowAlreadyClosed) {
            for (Consumer<LateEvent> l : droppedLateListeners) {
                l.accept(late);
            }
        }
    }

    /** Advance to {@code newWatermark}: close every partition window whose end <= wm. */
    public synchronized void onWatermark(long newWatermark) {
        if (newWatermark == com.example.watermark.watermarks.Watermark.NO_WATERMARK) {
            return;
        }
        for (Map.Entry<String, TreeMapShim> entry : openWindowsByPartition.entrySet()) {
            String partition = entry.getKey();
            TreeMapShim windows = entry.getValue();
            // A window [s, s+size) closes when the watermark reaches its end:
            // wm >= s + size  <=>  s <= wm - size.
            List<Map.Entry<Long, Bucket>> closing =
                    new ArrayList<>(windows.raw().headMap(newWatermark - windowSizeMillis, true)
                            .entrySet());
            for (Map.Entry<Long, Bucket> e : closing) {
                Bucket b = e.getValue();
                windows.raw().remove(e.getKey());
                if (b.payloads.isEmpty()) {
                    continue; // empty windows never fire
                }
                WindowResult result = new WindowResult(
                        b.start, b.end, partition, b.payloads.size(), new ArrayList<>(b.payloads));
                results.add(result);
                for (Consumer<WindowResult> l : closeListeners) {
                    l.accept(result);
                }
            }
        }
    }

    /** All windows closed so far, in close order. */
    public synchronized List<WindowResult> getResults() {
        return new ArrayList<>(results);
    }

    /** Nothing to release; present for try-with-resources symmetry. */
    @Override
    public void close() {
    }
}
