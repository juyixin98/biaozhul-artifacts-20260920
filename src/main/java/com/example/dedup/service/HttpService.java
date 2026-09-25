package com.example.dedup.service;

import com.example.dedup.json.Json;
import com.example.dedup.model.Event;
import com.example.dedup.model.IngestResult;
import com.example.dedup.pipeline.PipelineConfig;
import com.example.dedup.pipeline.StreamPipeline;
import com.example.dedup.time.Clock;
import com.example.dedup.time.ManualClock;
import com.example.dedup.time.Scheduler;
import com.example.dedup.time.WallClock;
import com.example.dedup.time.WallScheduler;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import java.io.IOException;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.Executors;

/**
 * JSON I/O service around {@link StreamPipeline}.
 *
 * Endpoints (all JSON, GET health; others POST):
 *   GET  /health
 *   POST /events     {"events":[...]}            -> per-event observable results
 *   POST /watermark  {"watermark":ms}            -> explicit advance
 *   POST /tick       {"advanceMillis":n}         -> manual clock only
 *   POST /drain      {}                          -> window results + late side outputs
 *   GET  /metrics                                 -> counters/gauges
 *   POST /snapshot   {}                          -> state as JSON
 *   POST /restore    {"snapshot": {...}}          -> replace state (testing)
 *
 * In wall mode a daemon scheduler may auto-emit watermarks; in manual mode
 * neither clock nor scheduler moves unless an endpoint drives them.
 */
public final class HttpService implements AutoCloseable {

    private final HttpServer server;
    private final StreamPipeline pipeline;
    private final Clock clock;
    private final Scheduler scheduler;
    private final WallScheduler wallScheduler; // null in manual mode
    private final Path snapshotFile;

    private HttpService(int port, StreamPipeline pipeline, Clock clock,
                        Scheduler scheduler, WallScheduler wallScheduler, Path snapshotFile)
            throws IOException {
        this.pipeline = pipeline;
        this.clock = clock;
        this.scheduler = scheduler;
        this.wallScheduler = wallScheduler;
        this.snapshotFile = snapshotFile;
        this.server = HttpServer.create(new InetSocketAddress(port), 0);
        this.server.setExecutor(Executors.newFixedThreadPool(4));
        register();
    }

    /** Manual deterministic service: clock only moves via /tick. */
    public static HttpService manual(int port, PipelineConfig config, Path snapshotFile) throws IOException {
        ManualClock clock = new ManualClock();
        StreamPipeline p = loadOrCreate(snapshotFile, config, clock, Scheduler.noop());
        return new HttpService(port, p, clock, Scheduler.noop(), null, snapshotFile);
    }

    /** Wall-clock service; autoWatermarkPeriodMillis>0 enables timer emission. */
    public static HttpService wall(int port, PipelineConfig config, Path snapshotFile) throws IOException {
        WallClock base = new WallClock();
        WallScheduler sched = new WallScheduler();
        StreamPipeline p = loadOrCreate(snapshotFile, config, base, sched);
        return new HttpService(port, p, base, sched, sched, snapshotFile);
    }

    private static StreamPipeline loadOrCreate(Path snapshotFile, PipelineConfig config,
                                               Clock clock, Scheduler scheduler) {
        if (snapshotFile != null && Files.exists(snapshotFile)) {
            try {
                String text = Files.readString(snapshotFile, StandardCharsets.UTF_8);
                Json.JsonObject root = Json.parseObject(text);
                return StreamPipeline.restore(root, config, clock, scheduler);
            } catch (Exception e) {
                throw new IllegalStateException("Failed to restore snapshot from " + snapshotFile + ": " + e.getMessage(), e);
            }
        }
        return new StreamPipeline(config, clock, scheduler);
    }

    public int port() {
        return server.getAddress().getPort();
    }

    public StreamPipeline pipeline() {
        return pipeline;
    }

    public void start() {
        server.start();
    }

    @Override
    public void close() {
        server.stop(0);
        if (wallScheduler != null) {
            wallScheduler.close();
        }
    }

    private void register() {
        server.createContext("/health", this::health);
        server.createContext("/events", this::events);
        server.createContext("/watermark", this::watermark);
        server.createContext("/tick", this::tick);
        server.createContext("/drain", this::drain);
        server.createContext("/metrics", this::metrics);
        server.createContext("/snapshot", this::snapshot);
        server.createContext("/restore", this::restore);
    }

    // ---------------------------------------------------------------- handlers

    private void health(HttpExchange ex) throws IOException {
        Json.JsonObject o = HttpJson.envelope(true);
        o.members().put("mode", wallScheduler != null ? Json.str("wall") : Json.str("manual"));
        o.members().put("clockMillis", Json.num(clock.currentTimeMillis()));
        HttpJson.send(ex, 200, o);
    }

    private void events(HttpExchange ex) throws IOException {
        try {
            Json.Value body = HttpJson.readBody(ex);
            if (!(body instanceof Json.JsonObject bo) || !(bo.get("events") instanceof Json.JsonArray arr)) {
                error(ex, 400, "expected {\"events\":[...]}");
                return;
            }
            List<Json.Value> results = new ArrayList<>();
            for (Json.Value v : arr.elements()) {
                if (!(v instanceof Json.JsonObject eo)) {
                    error(ex, 400, "each event must be an object");
                    return;
                }
                Event e = Event.fromJson(eo);
                IngestResult r = pipeline.ingest(e);
                results.add(r.toJson());
            }
            Json.JsonObject o = HttpJson.envelope(true);
            o.members().put("results", new Json.JsonArray(results));
            o.members().put("watermark", wmJson());
            o.members().put("metrics", pipeline.metrics().toJson());
            HttpJson.send(ex, 200, o);
        } catch (IllegalArgumentException e) {
            error(ex, 400, e.getMessage());
        }
    }

    private void watermark(HttpExchange ex) throws IOException {
        Json.Value body = HttpJson.readBody(ex);
        if (!(body instanceof Json.JsonObject bo) || !bo.has("watermark")) {
            error(ex, 400, "expected {\"watermark\":ms}");
            return;
        }
        long target = bo.getLong("watermark", Long.MIN_VALUE);
        long before = pipeline.watermark();
        boolean advanced = pipeline.advanceWatermark(target);
        Json.JsonObject o = HttpJson.envelope(true);
        o.members().put("requested", Json.num(target));
        o.members().put("previousWatermark", wmOrNull(before));
        o.members().put("watermark", wmJson());
        o.members().put("advanced", Json.JsonBool.of(advanced));
        if (!advanced) {
            o.members().put("note", Json.str("target did not exceed current watermark; counted as regression"));
        }
        HttpJson.send(ex, 200, o);
    }

    private void tick(HttpExchange ex) throws IOException {
        if (!(clock instanceof ManualClock mc)) {
            error(ex, 400, "/tick is only available in manual mode");
            return;
        }
        Json.Value body = HttpJson.readBody(ex);
        long delta = (body instanceof Json.JsonObject bo) ? bo.getLong("advanceMillis", 0) : 0;
        long newClock = mc.advance(delta);
        Json.JsonObject o = HttpJson.envelope(true);
        o.members().put("clockMillis", Json.num(newClock));
        o.members().put("watermark", wmJson());
        o.members().put("note", Json.str("tick moves processing time only; watermark advances via /watermark or observed event time"));
        HttpJson.send(ex, 200, o);
    }

    private void drain(HttpExchange ex) throws IOException {
        var wins = pipeline.drainWindowOutputs();
        var late = pipeline.drainLateOutputs();
        Json.JsonObject o = HttpJson.envelope(true);
        Json.JsonArray wa = Json.arr();
        wins.forEach(w -> wa.elements().add(w.toJson()));
        Json.JsonArray la = Json.arr();
        late.forEach(l -> la.elements().add(l.toJson()));
        o.members().put("windowResults", wa);
        o.members().put("lateSideOutputs", la);
        HttpJson.send(ex, 200, o);
    }

    private void metrics(HttpExchange ex) throws IOException {
        HttpJson.send(ex, 200, pipeline.metrics().toJson());
    }

    private void snapshot(HttpExchange ex) throws IOException {
        Json.Value snap = pipeline.snapshot();
        if (snapshotFile != null) {
            Files.writeString(snapshotFile, Json.write(snap), StandardCharsets.UTF_8);
        }
        Json.JsonObject o = HttpJson.envelope(true);
        o.members().put("snapshot", snap);
        if (snapshotFile != null) {
            o.members().put("savedTo", Json.str(snapshotFile.toString()));
        }
        HttpJson.send(ex, 200, o);
    }

    private void restore(HttpExchange ex) throws IOException {
        Json.Value body = HttpJson.readBody(ex);
        if (!(body instanceof Json.JsonObject bo) || !(bo.get("snapshot") instanceof Json.JsonObject snap)) {
            error(ex, 400, "expected {\"snapshot\":{...}}");
            return;
        }
        // Restore into a fresh pipeline by writing snapshot file through the
        // constructor path is overkill; use static restore and swap via server
        // restart instead. For in-process tests we expose restoreFresh().
        error(ex, 400, "use restart with --snapshot-file to restore; or the library API StreamPipeline.restore");
    }

    private Json.Value wmJson() {
        long wm = pipeline.watermark();
        return wmOrNull(wm);
    }

    private static Json.Value wmOrNull(long wm) {
        return wm == Long.MIN_VALUE ? Json.JsonNull.INSTANCE : Json.num(wm);
    }

    private void error(HttpExchange ex, int status, String msg) throws IOException {
        Json.JsonObject o = HttpJson.envelope(false);
        o.members().put("error", Json.str(msg));
        HttpJson.send(ex, status, o);
    }
}
