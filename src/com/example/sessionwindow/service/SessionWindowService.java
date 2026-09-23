package com.example.sessionwindow.service;

import com.example.sessionwindow.engine.SessionWindowEngine;
import com.example.sessionwindow.model.Event;
import com.example.sessionwindow.model.ResultRecord;
import com.example.sessionwindow.time.TimerHandle;
import com.example.sessionwindow.time.TimerService;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

/**
 * In-memory registry of named stateful pipelines.
 *
 * <p>Pipelines keep their state between requests; events and explicit
 * watermarks are fed incrementally and newly emitted ADD/RETRACT/SEALED/
 * PURGED/DROPPED records are returned per request and drained from the log.
 * An optional periodic timer can inject watermarks derived from the injected
 * {@link TimerService}.</p>
 */
public final class SessionWindowService {

    private final Map<String, Pipeline> pipelines = new ConcurrentHashMap<>();
    private final TimerService timerService;
    private final Map<String, TimerHandle> watermarkTimers = new ConcurrentHashMap<>();

    public SessionWindowService(TimerService timerService) {
        this.timerService = timerService;
    }

    public synchronized Pipeline createPipeline(String id, ServiceConfig config) {
        if (pipelines.containsKey(id)) {
            throw new IllegalArgumentException("pipeline already exists: " + id);
        }
        Pipeline pipeline = new Pipeline(id, config);
        pipelines.put(id, pipeline);
        return pipeline;
    }

    public Pipeline getPipeline(String id) {
        Pipeline p = pipelines.get(id);
        if (p == null) {
            throw new IllegalArgumentException("pipeline not found: " + id);
        }
        return p;
    }

    public synchronized void deletePipeline(String id) {
        TimerHandle handle = watermarkTimers.remove(id);
        if (handle != null) {
            handle.cancel();
        }
        if (pipelines.remove(id) == null) {
            throw new IllegalArgumentException("pipeline not found: " + id);
        }
    }

    public synchronized List<String> pipelineIds() {
        return new ArrayList<>(pipelines.keySet());
    }

    /** Feed a batch of events/watermarks into a pipeline; return drained records. */
    public List<ResultRecord> ingest(String id, List<BatchProcessor.Item> items) {
        Pipeline pipeline = getPipeline(id);
        synchronized (pipeline) {
            for (BatchProcessor.Item item : items) {
                if (item.isWatermark()) {
                    pipeline.processWatermark(item.watermark());
                } else {
                    pipeline.processEvent(item.event());
                }
            }
            return pipeline.drainRecords();
        }
    }

    /** Operational snapshot: pipeline status and currently retained state. */
    public Map<String, Object> status(String id) {
        Pipeline pipeline = getPipeline(id);
        synchronized (pipeline) {
            SessionWindowEngine engine = pipeline.engine();
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("pipelineId", id);
            m.put("config", configJson(pipeline.config()));
            m.put("watermark", engine.watermark());
            m.put("retainedKeys", engine.keyCount());
            m.put("retainedSessions", engine.totalSessionCount());
            m.put("pendingRecords", engine.records().size());
            List<Map<String, Object>> states = new ArrayList<>();
            for (SessionWindowEngine.SessionInfo info : engine.snapshot()) {
                Map<String, Object> s = new LinkedHashMap<>();
                s.put("key", info.key());
                s.put("sealed", info.sealed());
                s.put("aggregate", info.aggregate().toJson());
                states.add(s);
            }
            m.put("sessions", states);
            return m;
        }
    }

    public synchronized void shutdown() {
        for (TimerHandle h : watermarkTimers.values()) {
            h.cancel();
        }
        watermarkTimers.clear();
        pipelines.clear();
        timerService.shutdown();
    }

    /**
     * Register a periodic processing-time task that advances each pipeline's
     * watermark to the punctuated generator's current value. Demonstrates
     * timer-driven watermark injection without any external message system.
     */
    public synchronized void startPeriodicWatermarks(String id, long periodMillis) {
        Pipeline pipeline = getPipeline(id);
        if (!pipeline.config().autoWatermark()) {
            throw new IllegalStateException("pipeline " + id + " is not in autoWatermark mode");
        }
        if (watermarkTimers.containsKey(id)) {
            throw new IllegalStateException("periodic watermark already started for " + id);
        }
        TimerHandle handle = timerService.scheduleAtFixedRate(periodMillis, periodMillis, () -> {
            synchronized (pipeline) {
                // Periodic strategy: re-push the punctuated watermark.
                // (Watermark advancement normally happens punctuated on events;
                // a separate timer proves scheduling is fully injectable.)
                pipeline.injectWatermarkFromGenerator();
            }
        });
        watermarkTimers.put(id, handle);
    }

    static Map<String, Object> configJson(ServiceConfig c) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("gap", c.gap());
        m.put("allowedLateness", c.allowedLateness());
        m.put("autoWatermark", c.autoWatermark());
        m.put("outOfOrderness", c.outOfOrderness());
        return m;
    }

    /** Convenience for tests that feed one event. */
    public List<ResultRecord> ingestEvent(String id, Event event) {
        return ingest(id, List.of(BatchProcessor.Item.event(event)));
    }
}
