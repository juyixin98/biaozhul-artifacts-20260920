package com.example.timeout.api;

import com.example.timeout.core.TimeoutEntry;
import com.example.timeout.service.ServiceManager;
import com.example.timeout.service.TimeoutService;
import com.example.timeout.tz.TzdbInfo;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import java.io.IOException;
import java.io.InputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.time.Instant;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.Executors;

/**
 * 纯后端 JSON HTTP 服务（基于 JDK 内置 {@link HttpServer}，无前端、无第三方 Web 框架）。
 *
 * <p>所有请求在 {@link ServiceManager} 这一把稳定的锁上串行化，
 * 保证校时、推进、重启与超时判断彼此原子可见。
 */
public final class TimeoutHttpServer {

    private final HttpServer server;
    private final ObjectMapper mapper;
    private final ServiceManager manager;

    public TimeoutHttpServer(ServiceManager manager, int port) throws IOException {
        this.manager = manager;
        this.mapper = com.example.timeout.store.JsonMappers.mapper();
        this.server = HttpServer.create(new InetSocketAddress(port), 0);
        this.server.createContext("/", this::dispatch);
        this.server.setExecutor(Executors.newFixedThreadPool(4));
    }

    public void start() {
        server.start();
    }

    public void stop() {
        server.stop(0);
    }

    public int port() {
        return server.getAddress().getPort();
    }

    private void dispatch(HttpExchange ex) throws IOException {
        try {
            route(ex);
        } catch (BadRequestException e) {
            writeJson(ex, 400, ApiResponse.error("bad_request", e.getMessage()));
        } catch (IllegalArgumentException e) {
            writeJson(ex, 400, ApiResponse.error("invalid_argument", e.getMessage()));
        } catch (IllegalStateException e) {
            writeJson(ex, 409, ApiResponse.error("invalid_state", e.getMessage()));
        } catch (Exception e) {
            // 边界处不向调用方泄露内部细节；详细信息只输出到服务端控制台
            System.err.println("[ERROR] " + ex.getRequestMethod() + " " + ex.getRequestURI() + " -> " + e);
            writeJson(ex, 500, ApiResponse.error("internal", "internal server error"));
        } finally {
            ex.close();
        }
    }

    private void route(HttpExchange ex) throws IOException {
        String method = ex.getRequestMethod();
        String path = ex.getRequestURI().getPath();

        if ("GET".equals(method) && ("/health".equals(path) || "/".equals(path))) {
            handleHealth(ex);
        } else if ("GET".equals(method) && "/info".equals(path)) {
            handleInfo(ex);
        } else if ("GET".equals(method) && "/timeouts".equals(path)) {
            handleList(ex);
        } else if ("POST".equals(method) && "/timeouts".equals(path)) {
            handleSchedule(ex);
        } else if ("POST".equals(method) && path.startsWith("/clock/")) {
            handleClock(ex, path);
        } else if ("POST".equals(method) && "/persist".equals(path)) {
            synchronized (manager) {
                manager.current().persist();
            }
            writeJson(ex, 200, ApiResponse.ok(Map.of("persisted", true)));
        } else if ("POST".equals(method) && "/admin/restart".equals(path)) {
            handleRestart(ex);
        } else if ("GET".equals(method) && path.startsWith("/timeouts/")) {
            handleGet(ex, decode(path.substring("/timeouts/".length())));
        } else {
            writeJson(ex, 404,
                    ApiResponse.error("not_found", "no such endpoint: " + method + " " + path));
        }
    }

    private void handleHealth(HttpExchange ex) throws IOException {
        Map<String, Object> body = new LinkedHashMap<>();
        synchronized (manager) {
            body.put("status", "UP");
            body.put("clockType", manager.current().mode());
            body.put("generation", manager.generation());
        }
        writeJson(ex, 200, ApiResponse.ok(body));
    }

    private void handleInfo(HttpExchange ex) throws IOException {
        Map<String, Object> body = new LinkedHashMap<>();
        synchronized (manager) {
            TimeoutService svc = manager.current();
            body.put("clockType", svc.mode());
            body.put("generation", manager.generation());
            body.put("wallNow", svc.wallNow());
            body.put("monoNowNanos", svc.monoNow());
            body.put("tzdbVersion", TzdbInfo.version());
            body.put("javaVersion", TzdbInfo.javaVersion());
            body.put("timeZoneCount", java.time.ZoneId.getAvailableZoneIds().size());
            body.put("note", "monoNowNanos is process-local and meaningless across restarts; "
                    + "scheduled timeout durations are measured only by this monotonic clock");
        }
        writeJson(ex, 200, ApiResponse.ok(body));
    }

    private void handleList(HttpExchange ex) throws IOException {
        List<TimeoutJson> list;
        synchronized (manager) {
            list = manager.current().list().stream().map(this::toJson).toList();
        }
        writeJson(ex, 200, ApiResponse.ok(list));
    }

    private void handleGet(HttpExchange ex, String id) throws IOException {
        TimeoutEntry entry;
        synchronized (manager) {
            entry = manager.current().get(id);
        }
        writeJson(ex, 200, ApiResponse.ok(toJson(entry)));
    }

    private void handleSchedule(HttpExchange ex) throws IOException {
        JsonNode body = readJson(ex);
        TimeoutEntry entry;
        synchronized (manager) {
            ScheduleRequest req = ScheduleRequest.parse(body);
            String label = req.label().isBlank() ? req.id() : req.label();
            entry = manager.current().scheduleFromRule(req.id(), label, req.rule());
        }
        writeJson(ex, 201, ApiResponse.ok(toJson(entry)));
    }

    /** 虚拟时钟控制：/clock/tick、/clock/set-wall、/clock/advance-wall（后者 seconds 可为负）。 */
    private void handleClock(HttpExchange ex, String path) throws IOException {
        JsonNode body = readJson(ex);
        Map<String, Object> result = new LinkedHashMap<>();
        synchronized (manager) {
            TimeoutService svc = manager.current();
            switch (path) {
                case "/clock/tick" -> {
                    svc.tick(Duration.ofSeconds(longField(body, "seconds")));
                    result.put("effect", "real time elapsed; wall and monotonic clocks advanced together");
                }
                case "/clock/set-wall" -> {
                    svc.setWall(Instant.parse(textField(body, "wall")));
                    result.put("effect", "wall clock steered; monotonic reading unchanged; "
                            + "already-scheduled timeouts unaffected");
                }
                case "/clock/advance-wall" -> {
                    long seconds = longField(body, "seconds");
                    svc.advanceWall(Duration.ofSeconds(seconds));
                    result.put("effect", "wall clock adjusted by " + seconds
                            + "s; monotonic reading unchanged; timeouts unaffected");
                }
                default -> throw new BadRequestException("unknown clock operation: " + path);
            }
            result.put("wallNow", svc.wallNow());
            result.put("monoNowNanos", svc.monoNow());
            result.put("expiredIds", svc.list().stream()
                    .filter(TimeoutEntry::isExpired).map(TimeoutEntry::id).toList());
        }
        writeJson(ex, 200, ApiResponse.ok(result));
    }

    /**
     * 模拟进程重启：新时钟的单调读数归零，仅依据持久化的墙钟截止时间和当前墙钟重新换算。
     * 请求体可选 {@code "wallNow":"2026-...Z"}（仅虚拟模式）指定重启瞬间的墙钟，
     * 用来演示“停机期间墙钟被 NTP 调整后，重启必须重新计算”。
     */
    private void handleRestart(HttpExchange ex) throws IOException {
        JsonNode body = readJsonQuietly(ex);
        Instant wallNow = null;
        if (body.hasNonNull("wallNow")) {
            try {
                wallNow = Instant.parse(body.get("wallNow").asText());
            } catch (Exception e) {
                throw new BadRequestException("invalid wallNow, expected ISO-8601 instant: " + e.getMessage());
            }
        }
        Map<String, Object> result = new LinkedHashMap<>();
        synchronized (manager) {
            TimeoutService svc = manager.restart(wallNow);
            result.put("generation", manager.generation());
            result.put("wallNow", svc.wallNow());
            result.put("monoNowNanos", svc.monoNow());
            result.put("note", "monotonic clock reset to a new origin; every timeout was recomputed "
                    + "from its persisted wall-clock deadline against the current wall clock");
            result.put("timeouts", svc.list().stream().map(this::toJson).toList());
        }
        writeJson(ex, 200, ApiResponse.ok(result));
    }

    private TimeoutJson toJson(TimeoutEntry e) {
        TimeoutService svc = manager.current();
        long remaining = e.isExpired() ? 0L : svc.remainingMonotonic(e.id()).toNanos();
        return new TimeoutJson(e.id(), e.label(), e.status().name(), e.deadlineWall(),
                e.convertedAtWall(), e.convertedAtMonoNanos(), e.remainingAtConversionNanos(),
                e.fireMonoNanos(), remaining, e.firedAtWall(), e.firedAtMonoNanos(), e.ruleSource());
    }

    private JsonNode readJson(HttpExchange ex) throws IOException {
        try (InputStream in = ex.getRequestBody()) {
            byte[] bytes = in.readAllBytes();
            if (bytes.length == 0) {
                throw new BadRequestException("request body must be a JSON object");
            }
            return mapper.readTree(bytes);
        }
    }

    private JsonNode readJsonQuietly(HttpExchange ex) throws IOException {
        byte[] bytes = ex.getRequestBody().readAllBytes();
        return bytes.length == 0 ? mapper.createObjectNode() : mapper.readTree(bytes);
    }

    private static long longField(JsonNode node, String field) {
        JsonNode v = node.get(field);
        if (v == null || !v.isNumber()) {
            throw new BadRequestException("numeric field '" + field + "' is required");
        }
        return v.asLong();
    }

    private static String textField(JsonNode node, String field) {
        JsonNode v = node.get(field);
        if (v == null || !v.isTextual()) {
            throw new BadRequestException("text field '" + field + "' is required");
        }
        return v.asText();
    }

    private static String decode(String s) {
        return java.net.URLDecoder.decode(s, StandardCharsets.UTF_8);
    }

    private void writeJson(HttpExchange ex, int status, Object body) throws IOException {
        byte[] json = mapper.writerWithDefaultPrettyPrinter().writeValueAsBytes(body);
        ex.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        ex.sendResponseHeaders(status, json.length);
        ex.getResponseBody().write(json);
    }
}
