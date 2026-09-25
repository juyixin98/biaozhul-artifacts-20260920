package com.example.segmenter.service;

import com.example.segmenter.api.Segmenter;
import com.example.segmenter.json.Json;
import com.example.segmenter.model.Dictionary;
import com.example.segmenter.model.Segmentation;
import com.example.segmenter.model.Token;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.Executors;
import java.util.concurrent.ThreadPoolExecutor;

/**
 * 本地 JSON HTTP 服务（纯 JDK，无外部依赖，不调用任何外部搜索服务或大模型）。
 *
 * <ul>
 *   <li>POST /segment  {"text": "...", "n": 3, "version": "dict_v2"}</li>
 *   <li>GET  /versions —— 可用词典版本</li>
 *   <li>GET  /healthz  —— 健康检查</li>
 * </ul>
 */
public final class SegmentServer {

    /** 单请求文本上限（按 Unicode 码点计），防止 N 最佳内存被恶意输入放大。 */
    public static final int MAX_TEXT_CODEPOINTS = 100_000;

    private final HttpServer server;
    private final Map<String, Dictionary> dictionaries;
    private final double unknownCharCost;
    private final String defaultVersion;

    private SegmentServer(HttpServer server,
                          Map<String, Dictionary> dictionaries,
                          double unknownCharCost,
                          String defaultVersion) {
        this.server = server;
        this.dictionaries = dictionaries;
        this.unknownCharCost = unknownCharCost;
        this.defaultVersion = defaultVersion;
    }

    /**
     * 创建并启动服务。
     *
     * @param port 0 表示由操作系统分配端口（测试用）
     */
    public static SegmentServer start(int port,
                                      Map<String, Dictionary> dictionaries,
                                      double unknownCharCost,
                                      String defaultVersion) throws IOException {
        if (dictionaries.isEmpty()) {
            throw new IllegalArgumentException("at least one dictionary is required");
        }
        if (!dictionaries.containsKey(defaultVersion)) {
            throw new IllegalArgumentException("default version not found: " + defaultVersion);
        }
        HttpServer http = HttpServer.create(new InetSocketAddress("127.0.0.1", port), 0);
        SegmentServer instance = new SegmentServer(http,
                Collections.unmodifiableMap(new LinkedHashMap<>(dictionaries)),
                unknownCharCost, defaultVersion);

        http.createContext("/segment", instance::handleSegment);
        http.createContext("/versions", instance::handleVersions);
        http.createContext("/healthz", exchange -> {
            if (!"GET".equalsIgnoreCase(exchange.getRequestMethod())) {
                writeError(exchange, 405, "method_not_allowed", "use GET");
                return;
            }
            Map<String, Object> body = new LinkedHashMap<>();
            body.put("status", "ok");
            writeJson(exchange, 200, body);
        });

        ThreadPoolExecutor pool = (ThreadPoolExecutor) Executors.newFixedThreadPool(
                Math.max(2, Math.min(8, Runtime.getRuntime().availableProcessors())));
        http.setExecutor(pool);
        http.start();
        return instance;
    }

    public int port() {
        return server.getAddress().getPort();
    }

    public String defaultVersion() {
        return defaultVersion;
    }

    public void stop() {
        server.stop(0);
        ((java.util.concurrent.ThreadPoolExecutor) server.getExecutor()).shutdownNow();
    }

    // ---------- /segment ----------

    private void handleSegment(HttpExchange exchange) throws IOException {
        try {
            if (!"POST".equalsIgnoreCase(exchange.getRequestMethod())) {
                writeError(exchange, 405, "method_not_allowed", "use POST");
                return;
            }
            byte[] rawBody = exchange.getRequestBody().readAllBytes();
            Object parsed;
            try {
                parsed = Json.parse(new String(rawBody, StandardCharsets.UTF_8));
            } catch (Json.JsonException e) {
                writeError(exchange, 400, "invalid_json", e.getMessage());
                return;
            }
            if (!(parsed instanceof Map)) {
                writeError(exchange, 400, "invalid_request", "request body must be a JSON object");
                return;
            }
            @SuppressWarnings("unchecked")
            Map<String, Object> request = (Map<String, Object>) parsed;

            Object textValue = request.get("text");
            if (textValue == null) {
                writeError(exchange, 400, "missing_field", "field 'text' is required");
                return;
            }
            if (!(textValue instanceof String)) {
                writeError(exchange, 400, "invalid_field", "field 'text' must be a string");
                return;
            }
            String text = (String) textValue;
            if (text.codePoints().count() > MAX_TEXT_CODEPOINTS) {
                writeError(exchange, 413, "text_too_long",
                        "text exceeds " + MAX_TEXT_CODEPOINTS + " code points");
                return;
            }

            int n = 1;
            Object nValue = request.get("n");
            if (nValue != null) {
                if (!(nValue instanceof Number)) {
                    writeError(exchange, 400, "invalid_field",
                            "field 'n' must be an integer in [1, " + Segmenter.MAX_N_BEST + "]");
                    return;
                }
                Number num = (Number) nValue;
                if (num.doubleValue() != Math.rint(num.doubleValue())) {
                    writeError(exchange, 400, "invalid_field",
                            "field 'n' must be an integer in [1, " + Segmenter.MAX_N_BEST + "]");
                    return;
                }
                n = num.intValue();
                if (n < 1 || n > Segmenter.MAX_N_BEST) {
                    writeError(exchange, 400, "invalid_field",
                            "field 'n' must be an integer in [1, " + Segmenter.MAX_N_BEST + "]");
                    return;
                }
            }

            String version = defaultVersion;
            Object versionValue = request.get("version");
            if (versionValue != null) {
                if (!(versionValue instanceof String)) {
                    writeError(exchange, 400, "invalid_field", "field 'version' must be a string");
                    return;
                }
                version = (String) versionValue;
            }
            Dictionary dictionary = dictionaries.get(version);
            if (dictionary == null) {
                writeError(exchange, 404, "unknown_version",
                        "no such dictionary version: " + version
                                + " (available: " + String.join(", ", dictionaries.keySet()) + ")");
                return;
            }

            Segmenter segmenter = new Segmenter(dictionary, unknownCharCost);
            List<Segmentation> results = segmenter.segmentNBest(text, n);

            Map<String, Object> response = new LinkedHashMap<>();
            response.put("text", text);
            response.put("codePoints", text.codePointCount(0, text.length()));
            response.put("version", version);
            response.put("unknownCharCost", round6(unknownCharCost));
            response.put("results", resultsToJson(results));
            if (n == 1) {
                response.put("best", resultToJson(results.get(0)));
            }
            writeJson(exchange, 200, response);
        } catch (RuntimeException e) {
            writeError(exchange, 500, "internal_error", e.toString());
        }
    }

    private static List<Object> resultsToJson(List<Segmentation> results) {
        List<Object> list = new ArrayList<>(results.size());
        for (Segmentation s : results) {
            list.add(resultToJson(s));
        }
        return list;
    }

    private static Map<String, Object> resultToJson(Segmentation s) {
        Map<String, Object> item = new LinkedHashMap<>();
        item.put("rank", s.rank());
        item.put("totalCost", round6(s.totalCost()));
        List<Object> tokens = new ArrayList<>(s.tokenCount());
        List<Object> words = new ArrayList<>(s.tokenCount());
        int unknownCount = 0;
        for (Token t : s.tokens()) {
            Map<String, Object> token = new LinkedHashMap<>();
            token.put("word", t.word());
            token.put("cost", round6(t.cost()));
            token.put("unknown", t.unknown());
            tokens.add(token);
            words.add(t.word());
            if (t.unknown()) unknownCount++;
        }
        item.put("words", words);
        item.put("tokens", tokens);
        item.put("tokenCount", s.tokenCount());
        item.put("unknownCount", unknownCount);
        return item;
    }

    // ---------- /versions ----------

    private void handleVersions(HttpExchange exchange) throws IOException {
        if (!"GET".equalsIgnoreCase(exchange.getRequestMethod())) {
            writeError(exchange, 405, "method_not_allowed", "use GET");
            return;
        }
        Map<String, Object> response = new LinkedHashMap<>();
        response.put("default", defaultVersion);
        response.put("unknownCharCost", round6(unknownCharCost));
        List<Object> versions = new ArrayList<>();
        for (Dictionary d : dictionaries.values()) {
            Map<String, Object> info = new LinkedHashMap<>();
            info.put("version", d.version());
            info.put("wordCount", d.size());
            info.put("totalFrequency", d.totalFrequency() >= 0 ? d.totalFrequency() : null);
            versions.add(info);
        }
        response.put("versions", versions);
        writeJson(exchange, 200, response);
    }

    // ---------- 工具 ----------

    static double round6(double v) {
        return Math.round(v * 1_000_000.0) / 1_000_000.0;
    }

    private static void writeError(HttpExchange exchange, int status, String code, String message)
            throws IOException {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("error", code);
        body.put("message", message);
        writeJson(exchange, status, body);
    }

    private static void writeJson(HttpExchange exchange, int status, Object body) throws IOException {
        byte[] payload = Json.writePretty(body).getBytes(StandardCharsets.UTF_8);
        exchange.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        exchange.sendResponseHeaders(status, payload.length);
        try (OutputStream os = exchange.getResponseBody()) {
            os.write(payload);
        }
    }
}
