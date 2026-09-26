package hlc.server;

import hlc.HLCException;
import hlc.HLCFileStore;
import hlc.Json;
import hlc.LogicalCounterOverflowException;
import hlc.TimeZoneInfo;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.Executors;

/**
 * Small JSON-over-HTTP backend built only on the JDK ({@code com.sun.net.httpserver}).
 *
 * <table>
 *   <caption>Endpoints</caption>
 *   <tr><th>Method &amp; path</th><th>Purpose</th></tr>
 *   <tr><td>GET  /health</td><td>liveness + tzdb version</td></tr>
 *   <tr><td>GET  /info</td><td>algorithm notes and tzdb version</td></tr>
 *   <tr><td>POST /nodes</td><td>create a virtual node</td></tr>
 *   <tr><td>GET  /nodes</td><td>list nodes and current timestamps</td></tr>
 *   <tr><td>POST /tick</td><td>local event (physicalMicros required)</td></tr>
 *   <tr><td>POST /send</td><td>send event; response carries the stamped message</td></tr>
 *   <tr><td>POST /receive</td><td>merge an incoming message timestamp</td></tr>
 *   <tr><td>GET  /snapshot?node=</td><td>read current timestamp</td></tr>
 *   <tr><td>POST /restore</td><td>restore a node from persisted state</td></tr>
 *   <tr><td>POST /persist/save</td><td>atomically write all node clocks</td></tr>
 *   <tr><td>POST /persist/load</td><td>reload clocks from the state file</td></tr>
 *   <tr><td>POST /simulate</td><td>run a fixed interleaving scenario and check causality</td></tr>
 * </table>
 */
public final class HLCHttpServer {

    private final HttpServer server;
    private final ApiService api;

    private HLCHttpServer(HttpServer server, ApiService api) {
        this.server = server;
        this.api = api;
    }

    public static HLCHttpServer start(int port, Path stateFile) throws IOException {
        HttpServer http = HttpServer.create(new InetSocketAddress(port), 0);
        ApiService api = new ApiService(new HLCFileStore(stateFile));
        HLCHttpServer app = new HLCHttpServer(http, api);
        app.routes(http);
        http.setExecutor(Executors.newFixedThreadPool(4, r -> {
            Thread t = new Thread(r, "hlc-http");
            t.setDaemon(true);
            return t;
        }));
        http.start();
        return app;
    }

    public int port() {
        return server.getAddress().getPort();
    }

    public void stop() {
        server.stop(0);
    }

    private void routes(HttpServer http) {
        http.createContext("/health", ex -> handleGet(ex, req -> Map.of(
                "status", "ok", "tzdb", TimeZoneInfo.version())));
        http.createContext("/info", ex -> handleGet(ex, req -> Map.of(
                "service", "hybrid-logical-clock",
                "timestampFormat", "<l-micros>:<counter>",
                "tzdb", TimeZoneInfo.version(),
                "defaultZone", TimeZoneInfo.defaultZone(),
                "note", "timestamps are timezone-independent; tzdb is recorded for provenance")));
        http.createContext("/nodes", ex -> {
            if ("GET".equals(ex.getRequestMethod())) {
                handleGet(ex, req -> api.listNodes());
            } else {
                handlePost(ex, api::createNode);
            }
        });
        http.createContext("/tick", ex -> handlePost(ex, api::local));
        http.createContext("/send", ex -> handlePost(ex, api::send));
        http.createContext("/receive", ex -> handlePost(ex, api::receive));
        http.createContext("/snapshot", ex -> handleGet(ex, api::snapshot));
        http.createContext("/restore", ex -> handlePost(ex, api::restore));
        http.createContext("/persist/save", ex -> handlePost(ex, req -> api.save()));
        http.createContext("/persist/load", ex -> handlePost(ex, req -> api.load()));
        http.createContext("/simulate", ex -> handlePost(ex, ScenarioService::simulate));
    }

    // ------------------------------------------------------------ exchange plumbing

    @FunctionalInterface
    interface Handler {
        Map<String, Object> handle(Map<String, Object> request) throws Exception;
    }

    private void handleGet(HttpExchange ex, Handler h) throws IOException {
        try {
            writeJson(ex, 200, h.handle(queryParams(ex.getRequestURI().getRawQuery())));
        } catch (Exception e) {
            writeError(ex, e);
        }
    }

    private void handlePost(HttpExchange ex, Handler h) throws IOException {
        try {
            String body = new String(ex.getRequestBody().readAllBytes(), StandardCharsets.UTF_8);
            // Endpoints such as /persist/save take no body; treat it as an empty object and
            // let each handler validate the fields it actually requires.
            Map<String, Object> req = body.isBlank() ? new LinkedHashMap<>()
                    : Json.parseObject(body);
            writeJson(ex, 200, h.handle(req));
        } catch (Exception e) {
            writeError(ex, e);
        }
    }

    private static Map<String, Object> queryParams(String rawQuery) {
        Map<String, Object> params = new LinkedHashMap<>();
        if (rawQuery != null && !rawQuery.isBlank()) {
            for (String pair : rawQuery.split("&")) {
                int eq = pair.indexOf('=');
                String k = eq < 0 ? pair : pair.substring(0, eq);
                String v = eq < 0 ? "" : pair.substring(eq + 1);
                params.put(java.net.URLDecoder.decode(k, StandardCharsets.UTF_8),
                        java.net.URLDecoder.decode(v, StandardCharsets.UTF_8));
            }
        }
        return params;
    }

    private static void writeJson(HttpExchange ex, int status, Map<String, Object> payload)
            throws IOException {
        byte[] bytes = Json.writePretty(payload).getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().add("Content-Type", "application/json; charset=utf-8");
        ex.sendResponseHeaders(status, bytes.length);
        try (OutputStream os = ex.getResponseBody()) {
            os.write(bytes);
        }
    }

    private static void writeError(HttpExchange ex, Throwable error) throws IOException {
        int status;
        String code;
        if (error instanceof LogicalCounterOverflowException) {
            status = 507; // Insufficient Storage — semantically "cannot produce next timestamp"
            code = "LOGICAL_COUNTER_OVERFLOW";
        } else if (error instanceof HLCException) {
            status = 400;
            code = "INVALID_REQUEST";
        } else {
            status = 500;
            code = "INTERNAL_ERROR";
        }
        Map<String, Object> err = new LinkedHashMap<>();
        err.put("success", false);
        err.put("error", code);
        err.put("message", error.getMessage() == null ? error.getClass().getSimpleName()
                : error.getMessage());
        byte[] bytes = Json.writePretty(err).getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().add("Content-Type", "application/json; charset=utf-8");
        ex.sendResponseHeaders(status, bytes.length);
        try (OutputStream os = ex.getResponseBody()) {
            os.write(bytes);
        }
    }

    // ------------------------------------------------------------ entry point

    public static void main(String[] args) throws Exception {
        int port = Integer.parseInt(System.getProperty("port",
                System.getenv().getOrDefault("HLC_PORT", "8080")));
        String state = System.getProperty("stateFile",
                System.getenv().getOrDefault("HLC_STATE", "data/hlc-state.properties"));
        HLCHttpServer app = start(port, Path.of(state));
        System.out.println("HLC backend listening on http://localhost:" + app.port()
                + " (state file: " + Path.of(state).toAbsolutePath() + ")");
        System.out.println("tzdb version: " + TimeZoneInfo.version()
                + ", default zone: " + TimeZoneInfo.defaultZone());
    }
}
