package com.example.sessionwindow.service;

import com.example.sessionwindow.engine.SessionWindowEngine;
import com.example.sessionwindow.engine.WatermarkGenerator;
import com.example.sessionwindow.model.Event;
import com.example.sessionwindow.model.ResultRecord;

import java.util.ArrayList;
import java.util.List;

/**
 * A named, stateful stream-processing pipeline.
 *
 * <p>Wraps a {@link SessionWindowEngine} and, optionally, a punctuated
 * {@link WatermarkGenerator}. Records accumulate in the pipeline until
 * {@link #drainRecords()} is consumed; open state can be inspected for
 * operational checks.</p>
 *
 * <p>Not thread-safe by itself; {@link SessionWindowService} synchronizes on
 * the pipeline.</p>
 */
public final class Pipeline {

    private final String id;
    private final ServiceConfig config;
    private final SessionWindowEngine engine;
    private final WatermarkGenerator watermarkGenerator;

    Pipeline(String id, ServiceConfig config) {
        this.id = id;
        this.config = config;
        this.engine = new SessionWindowEngine(config.gap(), config.allowedLateness());
        this.watermarkGenerator = config.autoWatermark()
                ? new WatermarkGenerator(config.outOfOrderness())
                : null;
    }

    public String id() {
        return id;
    }

    public ServiceConfig config() {
        return config;
    }

    public SessionWindowEngine engine() {
        return engine;
    }

    public long watermark() {
        return engine.watermark();
    }

    /** Feed one event; advances the punctuated watermark when configured. */
    public void processEvent(Event event) {
        engine.processEvent(event);
        if (watermarkGenerator != null) {
            long proposed = watermarkGenerator.onEvent(event);
            if (proposed != Long.MIN_VALUE) {
                engine.processWatermark(proposed);
            }
        }
    }

    /** Explicit watermark injection (used in manual mode and by timers). */
    public void processWatermark(long watermark) {
        engine.processWatermark(watermark);
    }

    /**
     * Timer-driven watermark injection (periodic strategy). Pushes the
     * punctuated generator's current value if it has advanced; returns the
     * advanced value, or {@link Long#MIN_VALUE} when nothing changed.
     */
    public long injectWatermarkFromGenerator() {
        if (watermarkGenerator == null) {
            return Long.MIN_VALUE;
        }
        long w = watermarkGenerator.currentWatermark();
        if (w > engine.watermark()) {
            engine.processWatermark(w);
            return w;
        }
        return Long.MIN_VALUE;
    }

    public List<ResultRecord> drainRecords() {
        return engine.drainRecords();
    }

    public List<ResultRecord> peekRecords() {
        return new ArrayList<>(engine.records());
    }
}
