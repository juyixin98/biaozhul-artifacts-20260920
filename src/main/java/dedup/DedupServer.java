package dedup;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.Executors;

/**
 * HTTP front-end for {@link DedupService} using only the JDK's built-in HttpServer.
 *
 * Endpoints:
 *   GET  /health             -> 200 {"status":"ok"}
 *   GET  /state              -> 200 service description (watermarks, entry counts)
 *   POST /events             -> {"eventId","eventTime","routingVersion"}
 *                               200 {"status":"new"|"duplicate",...} | 409 stale version | 400 bad request
 *   POST /watermark          -> {"partition","watermark","routingVersion"} -> 200 {"evicted":N}
 *   POST /migration/export   -> {"partition","routingVersion"} -> 200 snapshot JSON
 *   POST /migration/import   -> snapshot JSON -> 200 {"imported":true} | 409 stale version
 *   POST /migration/bump     -> {} -> 200 {"routingVersion":N}  (operator: ownership changed)
 */
public class DedupServer {

    private final HttpServer server;
    private final DedupService service;
    private final java.util.concurrent.ExecutorService executor;

    public DedupServer(int port, DedupService service) throws IOException {
        this.service = service;
        this.server = HttpServer.create(new InetSocketAddress(port), 0);
        this.executor = Executors.newFixedThreadPool(8);
        server.setExecutor(executor);

        server.createContext("/health", guarded(ex -> respond(ex, 200, Map.of("status", "ok"))));
        server.createContext("/state", guarded(ex -> respond(ex, 200, service.describe())));
        server.createContext("/events", guarded(this::handleEvent));
        server.createContext("/watermark", guarded(this::handleWatermark));
        server.createContext("/migration/export", guarded(this::handleExport));
        server.createContext("/migration/import", guarded(this::handleImport));
        server.createContext("/migration/bump", guarded(ex -> {
            requirePost(ex);
            respond(ex, 200, Map.of("routingVersion", service.bumpRoutingVersion()));
        }));
    }

    /** Converts known failures into JSON error responses instead of dropping the connection. */
    private com.sun.net.httpserver.HttpHandler guarded(HttpHandlerIO h) {
        return ex -> {
            try {
                h.handle(ex);
            } catch (StaleRoutingVersionException e) {
                respond(ex, 409, Map.of(
                        "error", "stale_routing_version",
                        "expected", e.expected(),
                        "actual", e.actual()));
            } catch (HttpException e) {
                respond(ex, e.status, Map.of("error", e.getMessage()));
            } catch (IllegalArgumentException | IndexOutOfBoundsException e) {
                respond(ex, 400, Map.of("error", e.getMessage()));
            } finally {
                ex.close();
            }
        };
    }

    @FunctionalInterface
    private interface HttpHandlerIO { void handle(HttpExchange ex) throws IOException; }

    public void start() { server.start(); }

    public void stop() {
        server.stop(0);
        executor.shutdownNow(); // HttpServer.stop() does not shut down a custom executor
    }

    public int port() { return server.getAddress().getPort(); }

    private void handleEvent(HttpExchange ex) throws IOException {
        requirePost(ex);
        Map<String, Object> body = Json.parseObject(readBody(ex));
        String eventId = requiredString(body, "eventId");
        long eventTime = requiredLong(body, "eventTime");
        long version = requiredLong(body, "routingVersion");
        DedupService.SubmitResult r = service.submit(eventId, eventTime, version);
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("status", r.status() == DedupService.Status.NEW ? "new" : "duplicate");
        out.put("partition", r.partition());
        out.put("watermark", r.watermark() == Long.MIN_VALUE ? null : r.watermark());
        respond(ex, 200, out);
    }

    private void handleWatermark(HttpExchange ex) throws IOException {
        requirePost(ex);
        Map<String, Object> body = Json.parseObject(readBody(ex));
        int partition = (int) requiredLong(body, "partition");
        long watermark = requiredLong(body, "watermark");
        long version = requiredLong(body, "routingVersion");
        int evicted = service.advanceWatermark(partition, watermark, version);
        respond(ex, 200, Map.of("partition", partition, "watermark", watermark, "evicted", evicted));
    }

    private void handleExport(HttpExchange ex) throws IOException {
        requirePost(ex);
        Map<String, Object> body = Json.parseObject(readBody(ex));
        int partition = (int) requiredLong(body, "partition");
        long version = requiredLong(body, "routingVersion");
        DedupService.PartitionSnapshot snap = service.exportPartition(partition, version);
        respond(ex, 200, DedupService.snapshotToJson(snap));
    }

    private void handleImport(HttpExchange ex) throws IOException {
        requirePost(ex);
        Map<String, Object> body = Json.parseObject(readBody(ex));
        DedupService.PartitionSnapshot snap = DedupService.snapshotFromJson(body);
        service.importPartition(snap);
        respond(ex, 200, Map.of("imported", true, "partition", snap.partition(),
                "routingVersion", service.routingVersion()));
    }

    // ---------- helpers ----------

    private static void requirePost(HttpExchange ex) throws IOException {
        if (!"POST".equalsIgnoreCase(ex.getRequestMethod())) {
            throw new HttpException(405, "method not allowed, use POST");
        }
    }

    private static String readBody(HttpExchange ex) throws IOException {
        try (InputStream in = ex.getRequestBody()) {
            return new String(in.readAllBytes(), StandardCharsets.UTF_8);
        }
    }

    private static String requiredString(Map<String, Object> body, String key) {
        Object v = body.get(key);
        if (!(v instanceof String s) || s.isEmpty()) {
            throw new HttpException(400, "missing or invalid '" + key + "'");
        }
        return s;
    }

    private static long requiredLong(Map<String, Object> body, String key) {
        Object v = body.get(key);
        if (!(v instanceof Number n)) {
            throw new HttpException(400, "missing or invalid '" + key + "'");
        }
        return n.longValue();
    }

    private void respond(HttpExchange ex, int status, Object body) throws IOException {
        byte[] bytes = Json.write(body).getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        ex.sendResponseHeaders(status, bytes.length);
        try (OutputStream os = ex.getResponseBody()) {
            os.write(bytes);
        }
    }

    /** Bad-request style exception carrying an HTTP status. */
    static final class HttpException extends RuntimeException {
        final int status;
        HttpException(int status, String message) {
            super(message);
            this.status = status;
        }
    }

    public static void main(String[] args) throws IOException {
        int port = intArg(args, "port", 8080);
        int partitions = intArg(args, "partitions", 16);
        long allowedLateness = longArg(args, "allowed-lateness-ms", 60_000);
        long retention = longArg(args, "retention-ms", 300_000);

        DedupService service = new DedupService(partitions, allowedLateness, retention);
        DedupServer srv = new DedupServer(port, service);
        srv.start();
        System.out.printf("dedup-server listening on :%d (partitions=%d, allowedLatenessMs=%d, retentionMs=%d, routingVersion=%d)%n",
                srv.port(), partitions, allowedLateness, retention, service.routingVersion());
    }

    private static int intArg(String[] args, String name, int dflt) { return (int) longArg(args, name, dflt); }

    private static long longArg(String[] args, String name, long dflt) {
        String prefix = "--" + name + "=";
        for (String a : args) {
            if (a.startsWith(prefix)) return Long.parseLong(a.substring(prefix.length()));
        }
        return dflt;
    }
}
