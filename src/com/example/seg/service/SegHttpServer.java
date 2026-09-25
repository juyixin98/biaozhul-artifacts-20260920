package com.example.seg.service;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import java.io.IOException;
import java.io.OutputStream;
import java.math.BigDecimal;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 基于 JDK 内置 com.sun.net.httpserver 的最小 JSON HTTP 服务。
 * 不调用任何外部搜索服务或大模型，全部逻辑在本地完成。
 *
 * 路由：
 *   GET  /api/dictionaries               词典版本列表
 *   POST /api/segment        {"dictionary":"v1","text":"...","k":1}
 *   POST /api/crosscheck     同上（text 限长 16），返回穷举 vs DP 对照结果
 */
public final class SegHttpServer {

    private static final int MAX_BODY_BYTES = 64 * 1024;

    private final SegService service;
    private HttpServer server;

    public SegHttpServer(SegService service) {
        this.service = service;
    }

    public int start(int port) throws IOException {
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", port), 0);
        server.createContext("/api/dictionaries", this::handleDictionaries);
        server.createContext("/api/segment", this::handleSegment);
        server.createContext("/api/crosscheck", this::handleCrosscheck);
        server.start();
        return server.getAddress().getPort();
    }

    public void stop() {
        if (server != null) {
            server.stop(0);
        }
    }

    private void handleDictionaries(HttpExchange ex) throws IOException {
        if (!"GET".equals(ex.getRequestMethod())) {
            writeError(ex, 405, "METHOD_NOT_ALLOWED", "只支持 GET");
            return;
        }
        writeResult(ex, service.listDictionaries());
    }

    private void handleSegment(HttpExchange ex) throws IOException {
        handlePost(ex, (dict, text, k) -> service.segment(dict, text, k));
    }

    private void handleCrosscheck(HttpExchange ex) throws IOException {
        handlePost(ex, (dict, text, k) -> service.crosscheck(dict, text, k));
    }

    private interface ServiceCall {
        SegService.Result apply(String dictionary, String text, Integer k);
    }

    private void handlePost(HttpExchange ex, ServiceCall call) throws IOException {
        if (!"POST".equals(ex.getRequestMethod())) {
            writeError(ex, 405, "METHOD_NOT_ALLOWED", "只支持 POST");
            return;
        }
        String raw;
        try {
            raw = readBody(ex);
        } catch (IOException e) {
            writeError(ex, 400, "BODY_TOO_LARGE", e.getMessage());
            return;
        }

        Object parsed;
        try {
            parsed = Json.parse(raw);
        } catch (Json.JsonException e) {
            writeError(ex, 400, "INVALID_JSON", e.getMessage());
            return;
        }
        if (!(parsed instanceof Map<?, ?> req)) {
            writeError(ex, 400, "INVALID_REQUEST", "请求体必须是 JSON 对象");
            return;
        }

        Object dictObj = req.get("dictionary");
        if (dictObj == null) {
            writeError(ex, 400, "MISSING_DICTIONARY", "缺少 dictionary 字段");
            return;
        }
        Object textObj = req.get("text");
        if (textObj != null && !(textObj instanceof String)) {
            writeError(ex, 400, "BAD_TEXT", "text 必须是字符串");
            return;
        }
        Integer k = null;
        Object kObj = req.get("k");
        if (kObj != null) {
            if (!(kObj instanceof BigDecimal bd)) {
                writeError(ex, 400, "BAD_K", "k 必须是整数");
                return;
            }
            try {
                k = bd.intValueExact();
            } catch (ArithmeticException e) {
                writeError(ex, 400, "BAD_K", "k 必须是整数");
                return;
            }
        }
        writeResult(ex, call.apply(String.valueOf(dictObj), (String) textObj, k));
    }

    private String readBody(HttpExchange ex) throws IOException {
        byte[] bytes = ex.getRequestBody().readNBytes(MAX_BODY_BYTES + 1);
        if (bytes.length > MAX_BODY_BYTES) {
            throw new IOException("请求体超过 " + MAX_BODY_BYTES + " 字节上限");
        }
        return new String(bytes, StandardCharsets.UTF_8);
    }

    private void writeResult(HttpExchange ex, SegService.Result result) throws IOException {
        writeJson(ex, result.httpStatus, result.body);
    }

    private void writeError(HttpExchange ex, int status, String code, String message)
            throws IOException {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("error", code);
        body.put("message", message);
        writeJson(ex, status, body);
    }

    private void writeJson(HttpExchange ex, int status, Object body) throws IOException {
        byte[] payload = Json.write(body).getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        ex.sendResponseHeaders(status, payload.length);
        try (OutputStream os = ex.getResponseBody()) {
            os.write(payload);
        }
    }

    // ------------------------------------------------------------------
    // 独立启动：java ... SegHttpServer [port] [dictDir]
    // ------------------------------------------------------------------

    public static void main(String[] args) throws Exception {
        int port = args.length >= 1 ? Integer.parseInt(args[0]) : 8080;
        String dictDir = args.length >= 2 ? args[1] : "data/dicts";

        com.example.seg.dict.DictionaryRegistry registry =
                com.example.seg.dict.DictionaryRegistry.loadDirectory(
                        java.nio.file.Paths.get(dictDir));
        SegHttpServer http = new SegHttpServer(new SegService(registry));
        int actualPort = http.start(port);
        System.out.println("分词 JSON 服务已启动: http://127.0.0.1:" + actualPort);
        System.out.println("词典目录: " + dictDir + "，版本: " + registry.versions());
        System.out.println("端点: GET /api/dictionaries | POST /api/segment | POST /api/crosscheck");
    }
}
