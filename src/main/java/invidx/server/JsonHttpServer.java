package invidx.server;

import com.fasterxml.jackson.databind.ObjectMapper;
import invidx.engine.IndexStats;
import invidx.engine.InvertedIndex;
import invidx.model.DocKey;
import invidx.search.Hit;
import invidx.search.Query;
import invidx.search.Snapshot;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.URLDecoder;
import java.nio.charset.StandardCharsets;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.Executors;

/**
 * Small JSON/HTTP front-end over {@link InvertedIndex}, backed solely by
 * the JDK's built-in {@link HttpServer}. No external services are called.
 *
 * <table>
 *   <caption>Endpoints</caption>
 *   <tr><td>POST /documents</td><td>{@code {"text": "...", "id": 7?}}</td></tr>
 *   <tr><td>GET  /documents/{id}</td><td>current revision of an id</td></tr>
 *   <tr><td>DELETE /documents/{id}</td><td>delete current revision</td></tr>
 *   <tr><td>GET  /search?q=...</td><td>term or AND:/OR: boolean query</td></tr>
 *   <tr><td>POST /search</td><td>{@code {"q": "AND:a,b"}}</td></tr>
 *   <tr><td>POST /flush</td><td>force a RAM-to-segment flush</td></tr>
 *   <tr><td>POST /merge</td><td>merge all segments</td></tr>
 *   <tr><td>GET  /stats</td><td>segment/tombstone counters</td></tr>
 * </table>
 */
public final class JsonHttpServer implements AutoCloseable {

    private final HttpServer server;
    private final ObjectMapper mapper = new ObjectMapper();

    public JsonHttpServer(InvertedIndex index, int port) throws IOException {
        this.server = HttpServer.create(new InetSocketAddress("127.0.0.1", port), 0);
        server.setExecutor(Executors.newFixedThreadPool(8));
        register(index);
    }

    public int port() {
        return server.getAddress().getPort();
    }

    public void start() {
        server.start();
    }

    @Override
    public void close() {
        server.stop(0);
    }

    private void register(InvertedIndex index) {
        server.createContext("/documents", exchange -> {
            try {
                routeDocuments(index, exchange);
            } catch (Exception e) {
                sendError(exchange, e);
            }
        });
        server.createContext("/search", exchange -> {
            try {
                routeSearch(index, exchange);
            } catch (Exception e) {
                sendError(exchange, e);
            }
        });
        server.createContext("/flush", exchange -> simplePost(exchange, () -> {
            index.flush();
            return Map.of("flushed", true);
        }));
        server.createContext("/merge", exchange -> simplePost(exchange, () -> {
            int merged = index.forceMerge();
            return Map.of("mergedSegments", merged);
        }));
        server.createContext("/stats", exchange -> simpleGet(exchange, () -> statsJson(index.stats())));
    }

    // ------------------------------------------------------------ documents

    private void routeDocuments(InvertedIndex index, HttpExchange exchange) throws IOException {
        String path = exchange.getRequestURI().getPath();
        String method = exchange.getRequestMethod();
        String suffix = path.substring("/documents".length());

        if (suffix.isEmpty() || suffix.equals("/")) {
            if (method.equals("POST")) {
                Map<?, ?> body = readJsonBody(exchange);
                Object textObj = body.get("text");
                if (!(textObj instanceof String text) || text.isBlank()) {
                    throw new ApiException(400, "field 'text' must be a non-empty string");
                }
                DocKey key;
                Object idObj = body.get("id");
                if (idObj instanceof Number n) {
                    key = index.putDocument(n.intValue(), text);
                } else {
                    key = index.addDocument(text);
                }
                sendJson(exchange, 200, Map.of("id", key.id(), "gen", key.gen()));
                return;
            }
            throw new ApiException(405, "use POST /documents");
        }

        int id = parseId(suffix);
        switch (method) {
            case "GET" -> {
                Snapshot.LiveDoc doc = index.get(id);
                if (doc == null) {
                    throw new ApiException(404, "no live document with id " + id);
                }
                sendJson(exchange, 200, Map.of(
                        "id", id, "gen", doc.gen(), "segment", doc.segmentName(),
                        "text", doc.text()));
            }
            case "DELETE" -> {
                boolean deleted = index.deleteDocument(id);
                sendJson(exchange, deleted ? 200 : 404,
                        Map.of("id", id, "deleted", deleted));
            }
            default -> throw new ApiException(405, "unsupported method " + method);
        }
    }

    // --------------------------------------------------------------- search

    private void routeSearch(InvertedIndex index, HttpExchange exchange) throws IOException {
        String q;
        if (exchange.getRequestMethod().equals("POST")) {
            Map<?, ?> body = readJsonBody(exchange);
            Object qObj = body.get("q");
            if (!(qObj instanceof String s) || s.isBlank()) {
                throw new ApiException(400, "field 'q' required");
            }
            q = s;
        } else {
            q = queryParam(exchange, "q");
            if (q == null || q.isBlank()) {
                throw new ApiException(400, "query parameter 'q' required");
            }
        }
        Query query = Query.parse(q);
        List<Hit> hits = index.search(query);
        Map<String, Object> response = new LinkedHashMap<>();
        response.put("query", q);
        response.put("total", hits.size());
        response.put("hits", hits.stream().map(h -> {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("id", h.id());
            m.put("gen", h.gen());
            m.put("text", h.text());
            return m;
        }).toList());
        sendJson(exchange, 200, response);
    }

    private void simplePost(HttpExchange exchange, java.util.function.Supplier<Object> action) {
        try {
            if (!exchange.getRequestMethod().equals("POST")) {
                throw new ApiException(405, "POST required");
            }
            Object result = action.get();
            sendJson(exchange, 200, result);
        } catch (Exception e) {
            sendError(exchange, e);
        }
    }

    private void simpleGet(HttpExchange exchange, java.util.function.Supplier<Object> action) {
        try {
            if (!exchange.getRequestMethod().equals("GET")) {
                throw new ApiException(405, "GET required");
            }
            sendJson(exchange, 200, action.get());
        } catch (Exception e) {
            sendError(exchange, e);
        }
    }

    // ---------------------------------------------------------------- utils

    private Map<String, Object> statsJson(IndexStats s) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("publishedSegments", s.publishedSegments());
        m.put("segmentNames", s.segmentNames());
        m.put("bufferedDocs", s.bufferedDocs());
        m.put("tombstones", s.tombstoneCount());
        m.put("liveDocs", s.liveDocCount());
        m.put("segmentSeq", s.segmentSeq());
        m.put("manifestVersion", s.manifestVersion());
        return m;
    }

    private Map<?, ?> readJsonBody(HttpExchange exchange) throws IOException {
        byte[] body = exchange.getRequestBody().readAllBytes();
        if (body.length == 0) {
            return Map.of();
        }
        return mapper.readValue(body, Map.class);
    }

    private static int parseId(String suffix) {
        String raw = suffix.startsWith("/") ? suffix.substring(1) : suffix;
        if (raw.endsWith("/")) {
            raw = raw.substring(0, raw.length() - 1);
        }
        try {
            int id = Integer.parseInt(raw);
            if (id < 0) {
                throw new NumberFormatException();
            }
            return id;
        } catch (NumberFormatException e) {
            throw new ApiException(400, "bad document id: " + raw);
        }
    }

    private static String queryParam(HttpExchange exchange, String name) {
        String raw = exchange.getRequestURI().getRawQuery();
        if (raw == null) {
            return null;
        }
        for (String pair : raw.split("&")) {
            int eq = pair.indexOf('=');
            String key = eq < 0 ? pair : pair.substring(0, eq);
            if (key.equals(name)) {
                String value = eq < 0 ? "" : pair.substring(eq + 1);
                return URLDecoder.decode(value, StandardCharsets.UTF_8);
            }
        }
        return null;
    }

    private void sendJson(HttpExchange exchange, int status, Object payload) throws IOException {
        byte[] data = mapper.writerWithDefaultPrettyPrinter().writeValueAsBytes(payload);
        exchange.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        exchange.sendResponseHeaders(status, data.length);
        try (OutputStream os = exchange.getResponseBody()) {
            os.write(data);
        }
    }

    private void sendError(HttpExchange exchange, Throwable error) {
        try {
            int status = error instanceof ApiException api ? api.status() : 500;
            String message = error.getMessage() == null ? error.getClass().getSimpleName() : error.getMessage();
            sendJson(exchange, status, Map.of("error", message, "status", status));
        } catch (IOException ignored) {
            exchange.close();
        }
    }

    private static final class ApiException extends RuntimeException {
        private final int status;

        ApiException(int status, String message) {
            super(message);
            this.status = status;
        }

        int status() {
            return status;
        }
    }
}
