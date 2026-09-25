package neardup.server;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * Local JSON HTTP service (JDK built-in {@link HttpServer}, no frameworks,
 * no network calls to anything external).
 *
 * Routes:
 *   GET  /health
 *   GET  /corpus
 *   POST /cluster                 body: {} or {"threshold":0.6} or {"texts":[...]}
 */
public final class HttpApiServer {

    private final int port;
    private HttpServer server;

    public HttpApiServer(int port) {
        this.port = port;
    }

    public void start() throws IOException {
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", port), 0);
        server.createContext("/health", this::handleHealth);
        server.createContext("/corpus", this::handleCorpus);
        server.createContext("/cluster", this::handleCluster);
        server.setExecutor(null);
        server.start();
    }

    public int boundPort() {
        return server.getAddress().getPort();
    }

    public void stop() {
        if (server != null) {
            server.stop(0);
        }
    }

    private void handleHealth(HttpExchange ex) throws IOException {
        if ("GET".equals(ex.getRequestMethod())) {
            writeJson(ex, 200, ApiService.health());
        } else {
            writeError(ex, 405, "method not allowed: use GET");
        }
    }

    private void handleCorpus(HttpExchange ex) throws IOException {
        if ("GET".equals(ex.getRequestMethod())) {
            writeJson(ex, 200, ApiService.corpus());
        } else {
            writeError(ex, 405, "method not allowed: use GET");
        }
    }

    private void handleCluster(HttpExchange ex) throws IOException {
        if (!"POST".equals(ex.getRequestMethod())) {
            writeError(ex, 405, "method not allowed: use POST");
            return;
        }
        Object body;
        try (InputStream is = ex.getRequestBody()) {
            String raw = new String(is.readAllBytes(), StandardCharsets.UTF_8);
            if (raw.isBlank()) {
                body = new LinkedHashMap<String, Object>();
            } else {
                body = Json.parse(raw);
            }
        } catch (Exception e) {
            writeError(ex, 400, "invalid request JSON: " + e.getMessage());
            return;
        }
        try {
            Map<String, Object> result = ApiService.cluster(body);
            writeJson(ex, 200, result);
        } catch (IllegalArgumentException e) {
            writeError(ex, 400, e.getMessage());
        }
    }

    private static void writeJson(HttpExchange ex, int code, Object payload) throws IOException {
        byte[] bytes = Json.stringify(payload).getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().add("Content-Type", "application/json; charset=utf-8");
        ex.sendResponseHeaders(code, bytes.length);
        try (OutputStream os = ex.getResponseBody()) {
            os.write(bytes);
        }
    }

    private static void writeError(HttpExchange ex, int code, String message) throws IOException {
        Map<String, Object> err = new LinkedHashMap<>();
        err.put("error", true);
        err.put("status", code);
        err.put("message", message);
        writeJson(ex, code, err);
    }
}
