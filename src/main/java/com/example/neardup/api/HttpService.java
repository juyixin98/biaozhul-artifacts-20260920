package com.example.neardup.api;

import com.example.neardup.ClusterResult;
import com.example.neardup.Clusterer;
import com.example.neardup.CorpusGenerator;
import com.example.neardup.Document;
import com.example.neardup.NearDupConfig;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.util.List;
import java.util.Map;
import java.util.concurrent.Executors;

/**
 * Minimal JSON HTTP service on the JDK built-in HttpServer. No external
 * search service, no model calls — everything is computed in-process.
 *
 * <pre>
 *   GET  /health          -> {"status":"ok"}
 *   GET  /corpus/sample   -> {"documents":[...]}   the built-in synthetic corpus
 *   POST /cluster         -> body {"documents":[{"id","text"}...], "threshold"?}
 *   POST /cluster/sample  -> body {"threshold"?}   clusters the built-in corpus
 * </pre>
 */
public final class HttpService {

    private static final int MAX_BODY_BYTES = 4 * 1024 * 1024;
    private static final int MAX_TEXT_LENGTH = 200_000;

    private final HttpServer server;
    private final ObjectMapper mapper = new ObjectMapper();
    private final Clusterer clusterer = new Clusterer();

    public HttpService(int port) throws IOException {
        server = HttpServer.create(new InetSocketAddress(port), 0);
        server.createContext("/health", this::handleHealth);
        server.createContext("/corpus/sample", this::handleCorpusSample);
        server.createContext("/cluster", this::handleCluster);
        server.createContext("/cluster/sample", this::handleClusterSample);
        server.setExecutor(Executors.newFixedThreadPool(4));
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

    private void handleHealth(HttpExchange exchange) throws IOException {
        if (!requireMethod(exchange, "GET")) {
            return;
        }
        sendJson(exchange, 200, Map.of("status", "ok"));
    }

    private void handleCorpusSample(HttpExchange exchange) throws IOException {
        if (!requireMethod(exchange, "GET")) {
            return;
        }
        sendJson(exchange, 200, Map.of("documents", CorpusGenerator.generate()));
    }

    private void handleCluster(HttpExchange exchange) throws IOException {
        if (!requireMethod(exchange, "POST")) {
            return;
        }
        try {
            ClusterRequest request = mapper.readValue(readBody(exchange), ClusterRequest.class);
            validateTexts(request.documents());
            ClusterResult result = clusterer.cluster(request.documents(), configFor(request.threshold()));
            sendJson(exchange, 200, result);
        } catch (IllegalArgumentException e) {
            sendJson(exchange, 400, Map.of("error", e.getMessage()));
        } catch (IOException e) {
            sendJson(exchange, 400, Map.of("error", "invalid JSON body: " + e.getMessage()));
        }
    }

    private void handleClusterSample(HttpExchange exchange) throws IOException {
        if (!requireMethod(exchange, "POST")) {
            return;
        }
        try {
            Double threshold = null;
            byte[] body = readBody(exchange);
            if (body.length > 0) {
                threshold = mapper.readTree(body).path("threshold").isNumber()
                        ? mapper.readTree(body).path("threshold").doubleValue()
                        : null;
            }
            ClusterResult result = clusterer.cluster(CorpusGenerator.generate(), configFor(threshold));
            sendJson(exchange, 200, result);
        } catch (IllegalArgumentException e) {
            sendJson(exchange, 400, Map.of("error", e.getMessage()));
        }
    }

    private static NearDupConfig configFor(Double threshold) {
        NearDupConfig config = NearDupConfig.defaults();
        return threshold == null ? config : config.withThreshold(threshold);
    }

    private static void validateTexts(List<Document> documents) {
        if (documents == null) {
            throw new IllegalArgumentException("documents field is required");
        }
        for (Document doc : documents) {
            if (doc != null && doc.text() != null && doc.text().length() > MAX_TEXT_LENGTH) {
                throw new IllegalArgumentException(
                        "document " + doc.id() + " exceeds max text length " + MAX_TEXT_LENGTH);
            }
        }
    }

    private boolean requireMethod(HttpExchange exchange, String method) throws IOException {
        if (!exchange.getRequestMethod().equalsIgnoreCase(method)) {
            sendJson(exchange, 405, Map.of("error", "method not allowed, use " + method));
            return false;
        }
        return true;
    }

    private static byte[] readBody(HttpExchange exchange) throws IOException {
        byte[] body = exchange.getRequestBody().readNBytes(MAX_BODY_BYTES + 1);
        if (body.length > MAX_BODY_BYTES) {
            throw new IllegalArgumentException("request body too large");
        }
        return body;
    }

    private void sendJson(HttpExchange exchange, int status, Object payload) throws IOException {
        byte[] bytes = mapper.writeValueAsBytes(payload);
        exchange.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        exchange.sendResponseHeaders(status, bytes.length);
        try (OutputStream out = exchange.getResponseBody()) {
            out.write(bytes);
        }
    }
}
