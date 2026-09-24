package phraseindex;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.Executors;

/**
 * Pure-JDK HTTP API ({@code com.sun.net.httpserver.HttpServer}, no
 * third-party dependencies) over {@link InvertedIndex}.
 *
 * <pre>
 * GET    /health
 * GET    /                        service description
 * PUT    /documents/{id}          raw UTF-8 body = document text (insert/replace)
 * POST   /documents/bulk          {"docs":[{"id":1,"text":"..."}, ...]}
 * GET    /documents/{id}          stored document text
 * DELETE /documents/{id}          delete document and all its positions
 * POST   /search                  {"query":"..."} ; "includePositions":true
 * </pre>
 */
public final class HttpServerApp {

    static final class ApiException extends RuntimeException {
        final int statusCode;

        ApiException(int statusCode, String message) {
            super(message);
            this.statusCode = statusCode;
        }
    }

    private final InvertedIndex index;
    private HttpServer server;

    public HttpServerApp(InvertedIndex index) {
        this.index = index;
    }

    public void start(int port) throws IOException {
        server = HttpServer.create(new InetSocketAddress(port), 0);
        server.createContext("/", this::route);
        java.util.concurrent.ThreadFactory daemonFactory = r -> {
            Thread t = new Thread(r, "http-worker");
            t.setDaemon(true);
            return t;
        };
        server.setExecutor(Executors.newFixedThreadPool(8, daemonFactory));
        server.start();
    }

    public void stop() {
        if (server != null) {
            server.stop(0);
        }
    }

    public int getPort() {
        return server == null ? -1 : server.getAddress().getPort();
    }

    private void route(HttpExchange exchange) {
        try {
            String path = exchange.getRequestURI().getPath();
            String method = exchange.getRequestMethod();
            switch (method) {
                case "GET" -> {
                    if (path.equals("/health")) {
                        sendJson(exchange, 200, Map.of("status", "ok", "documents", index.size()));
                    } else if (path.equals("/")) {
                        sendJson(exchange, 200, serviceInfo());
                    } else if (path.startsWith("/documents/")) {
                        handleGetDoc(exchange, path.substring("/documents/".length()));
                    } else {
                        throw new ApiException(404, "no such endpoint: " + method + " " + path);
                    }
                }
                case "PUT" -> {
                    if (path.startsWith("/documents/")) {
                        handlePutDoc(exchange, path.substring("/documents/".length()));
                    } else {
                        throw new ApiException(404, "no such endpoint: " + method + " " + path);
                    }
                }
                case "POST" -> {
                    if (path.equals("/documents/bulk")) {
                        handleBulk(exchange);
                    } else if (path.equals("/search")) {
                        handleSearch(exchange);
                    } else {
                        throw new ApiException(404, "no such endpoint: " + method + " " + path);
                    }
                }
                case "DELETE" -> {
                    if (path.startsWith("/documents/")) {
                        handleDeleteDoc(exchange, path.substring("/documents/".length()));
                    } else {
                        throw new ApiException(404, "no such endpoint: " + method + " " + path);
                    }
                }
                default -> throw new ApiException(405, "method not allowed: " + method);
            }
        } catch (ApiException e) {
            safeError(exchange, e.statusCode, e.getMessage());
        } catch (Exception e) {
            safeError(exchange, 500, "internal error: " + e.getMessage());
        } finally {
            exchange.close();
        }
    }

    private Map<String, Object> serviceInfo() {
        Map<String, Object> info = new LinkedHashMap<>();
        info.put("service", "positional-phrase-index");
        info.put("tokenization", "whitespace split, lowercase; runs of whitespace collapse; empty docs allowed");
        info.put("querySyntax", "exact phrase in double quotes; AND/OR/NOT (case-insensitive); parentheses; bare word = single-term phrase; adjacent atoms = implicit AND");
        info.put("endpoints", List.of(
                "GET /health",
                "PUT /documents/{id} (raw UTF-8 text body; insert or replace)",
                "POST /documents/bulk {\"docs\":[{\"id\":1,\"text\":\"...\"}]}",
                "GET /documents/{id}",
                "DELETE /documents/{id}",
                "POST /search {\"query\":\"...\",\"includePositions\":false}"));
        return info;
    }

    private void handleGetDoc(HttpExchange exchange, String idPart) throws IOException {
        int docId = parseDocId(idPart);
        String text = index.get(docId);
        if (text == null) {
            throw new ApiException(404, "document not found: " + docId);
        }
        sendJson(exchange, 200, Map.of("id", docId, "text", text));
    }

    private void handlePutDoc(HttpExchange exchange, String idPart) throws IOException {
        int docId = parseDocId(idPart);
        String body = readBody(exchange);
        boolean existed = index.contains(docId);
        index.put(docId, body);
        sendJson(exchange, 200, Map.of(
                "id", docId,
                "action", existed ? "replaced" : "inserted",
                "tokens", Tokenizer.tokenize(body).size()));
    }

    @SuppressWarnings("unchecked")
    private void handleBulk(HttpExchange exchange) throws IOException {
        Object parsed = parseJsonObject(readBody(exchange));
        Object docs = ((Map<String, Object>) parsed).get("docs");
        if (!(docs instanceof List<?> list)) {
            throw new ApiException(400, "expected JSON object with a \"docs\" array");
        }
        List<Object> result = new ArrayList<>();
        for (Object item : list) {
            if (!(item instanceof Map<?, ?> doc)) {
                throw new ApiException(400, "each docs entry must be an object");
            }
            int docId = requireIntId(doc.get("id"));
            String text = doc.get("text") == null ? "" : String.valueOf(doc.get("text"));
            boolean existed = index.contains(docId);
            index.put(docId, text);
            Map<String, Object> one = new LinkedHashMap<>();
            one.put("id", docId);
            one.put("action", existed ? "replaced" : "inserted");
            one.put("tokens", Tokenizer.tokenize(text).size());
            result.add(one);
        }
        sendJson(exchange, 200, Map.of("accepted", result.size(), "results", result,
                "documents", index.size()));
    }

    private void handleDeleteDoc(HttpExchange exchange, String idPart) throws IOException {
        int docId = parseDocId(idPart);
        if (!index.remove(docId)) {
            throw new ApiException(404, "document not found: " + docId);
        }
        sendJson(exchange, 200, Map.of("id", docId, "action", "deleted",
                "documents", index.size()));
    }

    @SuppressWarnings("unchecked")
    private void handleSearch(HttpExchange exchange) throws IOException {
        Object parsed = parseJsonObject(readBody(exchange));
        Object queryText = ((Map<String, Object>) parsed).get("query");
        if (!(queryText instanceof String qs) || qs.isBlank()) {
            throw new ApiException(400, "missing non-empty \"query\" string");
        }
        boolean includePositions = Boolean.TRUE.equals(((Map<String, Object>) parsed).get("includePositions"));

        Query query;
        try {
            query = QueryParser.parse(qs);
        } catch (QueryParseException e) {
            throw new ApiException(400, "query parse error: " + e.getMessage());
        }

        Set<Integer> matches = index.matchingDocIds(query);
        List<Integer> docIds = new ArrayList<>(matches);
        java.util.Collections.sort(docIds);

        Map<String, Object> response = new LinkedHashMap<>();
        response.put("query", qs);
        response.put("count", docIds.size());
        response.put("docIds", docIds);

        if (includePositions && query instanceof Query.Phrase p) {
            Map<String, Object> positionsByDoc = new LinkedHashMap<>();
            for (Integer docId : docIds) {
                positionsByDoc.put(String.valueOf(docId), index.phrasePositions(docId, p.terms()));
            }
            response.put("positions", positionsByDoc);
        }
        sendJson(exchange, 200, response);
    }

    private static int parseDocId(String idPart) {
        if (idPart.isEmpty() || idPart.contains("/")) {
            throw new ApiException(400, "malformed document id in path");
        }
        return requireIntId(idPart);
    }

    private static int requireIntId(Object raw) {
        try {
            if (raw instanceof Number n) {
                long l = n.longValue();
                if (l < 0 || l != Math.floor(l)) {
                    throw new ApiException(400, "document id must be a non-negative integer");
                }
                return (int) l;
            }
            return Integer.parseInt(String.valueOf(raw));
        } catch (NumberFormatException e) {
            throw new ApiException(400, "document id must be a non-negative integer: " + raw);
        }
    }

    private static Object parseJsonObject(String body) {
        try {
            Object v = Json.parse(body);
            if (!(v instanceof Map<?, ?>)) {
                throw new ApiException(400, "expected a JSON object body");
            }
            return v;
        } catch (IllegalArgumentException e) {
            throw new ApiException(400, "invalid JSON: " + e.getMessage());
        }
    }

    private static String readBody(HttpExchange exchange) throws IOException {
        byte[] bytes = exchange.getRequestBody().readAllBytes();
        return new String(bytes, StandardCharsets.UTF_8);
    }

    private void safeError(HttpExchange exchange, int statusCode, String message) {
        try {
            sendJson(exchange, statusCode, Map.of("error", message, "status", statusCode));
        } catch (IOException ignored) {
            // best-effort error reply; the finally block closes the exchange
        }
    }

    private static void sendJson(HttpExchange exchange, int statusCode, Object body) throws IOException {
        byte[] payload = Json.write(body).getBytes(StandardCharsets.UTF_8);
        exchange.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        exchange.sendResponseHeaders(statusCode, payload.length);
        try (OutputStream os = exchange.getResponseBody()) {
            os.write(payload);
        }
    }

    public static void main(String[] args) throws Exception {
        int port = 8080;
        if (args.length > 0) {
            try {
                port = Integer.parseInt(args[0]);
            } catch (NumberFormatException e) {
                System.err.println("usage: HttpServerApp [port]");
                System.exit(2);
            }
        }
        HttpServerApp app = new HttpServerApp(new InvertedIndex());
        app.start(port);
        Runtime.getRuntime().addShutdownHook(new Thread(app::stop));
        System.out.println("positional phrase index listening on http://localhost:" + app.getPort());
    }
}
