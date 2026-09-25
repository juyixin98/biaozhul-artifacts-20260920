package com.example.ac.server;

import com.example.ac.Compiled;
import com.example.ac.EmptyPatternPolicy;
import com.example.ac.Engine;
import com.example.ac.Match;
import com.example.ac.Pattern;
import com.example.ac.StreamingMatcher;
import com.example.ac.corpus.CorpusProfile;
import com.example.ac.corpus.CorpusRunner;
import com.example.ac.corpus.SyntheticCorpus;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.Executors;

/**
 * 基于 JDK 内置 com.sun.net.httpserver 的 JSON HTTP 服务（零外部依赖、无外部搜索/大模型）。
 *
 * <ul>
 *   <li>POST /api/match                 一次性整体匹配</li>
 *   <li>POST /api/sessions              建立流式会话（编译模式 + 返回 sessionId）</li>
 *   <li>POST /api/sessions/{id}/feed    喂文本块（可多次）</li>
 *   <li>POST /api/sessions/{id}/finish  结束并返回尾部命中</li>
 *   <li>DELETE /api/sessions/{id}       主动释放会话</li>
 *   <li>POST /api/corpus/run            生成合成语料并分块跑匹配，返回统计</li>
 *   <li>GET  /api/corpus?profile=&...   生成合成语料</li>
 *   <li>GET  /api/health, /             健康检查 / 服务说明</li>
 * </ul>
 */
public final class HttpJsonServer {

    private static final int MAX_SESSIONS = 200;

    private final HttpServer server;
    private final java.util.concurrent.ExecutorService executor;
    private final ConcurrentHashMap<String, Session> sessions = new ConcurrentHashMap<>();

    public HttpJsonServer(int port, String hostname) throws IOException {
        InetSocketAddress addr = hostname == null
                ? new InetSocketAddress(port)
                : new InetSocketAddress(hostname, port);
        this.server = HttpServer.create(addr, 0);
        this.executor = Executors.newFixedThreadPool(8);
        server.createContext("/", this::route);
        server.setExecutor(executor);
    }

    public int getPort() {
        return server.getAddress().getPort();
    }

    public void start() {
        server.start();
    }

    public void stop(int delaySeconds) {
        server.stop(delaySeconds);
        executor.shutdownNow();
        try {
            if (!executor.awaitTermination(2, java.util.concurrent.TimeUnit.SECONDS)) {
                // 强制退出后仍有残留也不阻塞调用方；线程均为非守护时这是必要保障。
            }
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }

    // ---- 会话 ----

    private static final class Session {
        final Compiled compiled;
        final StreamingMatcher m;
        boolean finished;

        Session(Compiled compiled) {
            this.compiled = compiled;
            this.m = new StreamingMatcher(compiled);
        }
    }

    // ---- 路由 ----

    private void route(HttpExchange ex) throws IOException {
        try {
            String path = ex.getRequestURI().getPath();
            String method = ex.getRequestMethod();
            switch (path) {
                case "/" -> info(ex);
                case "/api/health" -> health(ex);
                case "/api/match" -> requirePost(ex, method, this::matchOnce);
                case "/api/sessions" -> requirePost(ex, method, this::createSession);
                case "/api/corpus/run" -> requirePost(ex, method, this::corpusRun);
                default -> {
                    if (path.startsWith("/api/sessions/")) {
                        sessionAction(ex, method, path.substring("/api/sessions/".length()));
                    } else if (path.equals("/api/corpus") && method.equals("GET")) {
                        corpusGet(ex);
                    } else {
                        sendError(ex, 404, "not found: " + path);
                    }
                }
            }
        } catch (JsonException je) {
            sendError(ex, 400, je.getMessage());
        } catch (IllegalArgumentException iae) {
            sendError(ex, 400, iae.getMessage());
        } catch (Exception e) {
            sendError(ex, 500, e.getClass().getSimpleName() + ": " + e.getMessage());
        } finally {
            ex.close();
        }
    }

    private interface BodyHandler {
        void handle(HttpExchange ex, Map<String, Object> body) throws IOException;
    }

    private void requirePost(HttpExchange ex, String method, BodyHandler h) throws IOException {
        if (!"POST".equals(method)) {
            sendError(ex, 405, "method not allowed, use POST");
            return;
        }
        Map<String, Object> body = JsonParser.parseObject(readBody(ex));
        h.handle(ex, body);
    }

    private void sessionAction(HttpExchange ex, String method, String rest) throws IOException {
        String[] parts = rest.split("/");
        if (parts.length == 1) {
            // DELETE /api/sessions/{id}
            String id = parts[0];
            if ("DELETE".equals(method)) {
                Session removed = sessions.remove(id);
                if (removed == null) {
                    sendError(ex, 404, "session not found: " + id);
                } else {
                    sendJson(ex, 200, Map.of("deleted", id));
                }
                return;
            }
            sendError(ex, 405, "use DELETE on /api/sessions/{id}");
            return;
        }
        if (parts.length != 2) {
            sendError(ex, 404, "expected /api/sessions/{id}/feed|finish");
            return;
        }
        String id = parts[0];
        String action = parts[1];
        Session session = sessions.get(id);
        if (session == null) {
            sendError(ex, 404, "session not found: " + id);
            return;
        }
        if (!"POST".equals(method)) {
            sendError(ex, 405, "use POST");
            return;
        }
        switch (action) {
            case "feed" -> feedSession(ex, session, JsonParser.parseObject(readBody(ex)));
            case "finish" -> finishSession(ex, session, id);
            default -> sendError(ex, 404, "unknown session action: " + action);
        }
    }

    // ---- handler 实现 ----

    private void info(HttpExchange ex) throws IOException {
        Map<String, Object> j = new LinkedHashMap<>();
        j.put("service", "streaming-aho-corasick");
        j.put("endpoints", List.of(
                "POST /api/match",
                "POST /api/sessions",
                "POST /api/sessions/{id}/feed",
                "POST /api/sessions/{id}/finish",
                "DELETE /api/sessions/{id}",
                "POST /api/corpus/run",
                "GET /api/corpus?profile=dna|sharedPrefix|unicode",
                "GET /api/health"));
        j.put("positions", "start/end are Unicode code point offsets; charStart/charEnd are UTF-16 offsets; ranges are half-open");
        sendJson(ex, 200, j);
    }

    private void health(HttpExchange ex) throws IOException {
        sendJson(ex, 200, Map.of("status", "ok", "activeSessions", sessions.size()));
    }

    private void matchOnce(HttpExchange ex, Map<String, Object> req) throws IOException {
        PatternsSpec spec = readPatterns(req);
        String text = str(req, "text", "");
        Compiled compiled = Engine.compile(spec.patterns, spec.policy);
        List<Match> matches = Engine.match(compiled, text);

        Map<String, Object> j = new LinkedHashMap<>();
        j.put("compiled", JsonViews.compiledView(compiled));
        j.put("codePointLength", text.codePointCount(0, text.length()));
        j.put("matchCount", matches.size());
        j.put("matches", JsonViews.matchViews(matches));
        sendJson(ex, 200, j);
    }

    private void createSession(HttpExchange ex, Map<String, Object> req) throws IOException {
        PatternsSpec spec = readPatterns(req);
        Compiled compiled = Engine.compile(spec.patterns, spec.policy);
        if (sessions.size() >= MAX_SESSIONS) {
            sessions.clear(); // 简单策略：压测/测试场景下淘汰全部旧会话
        }
        String id = "sess-" + Long.toHexString(System.nanoTime()) + "-" + Integer.toHexString(System.identityHashCode(compiled));
        sessions.put(id, new Session(compiled));

        Map<String, Object> j = new LinkedHashMap<>();
        j.put("sessionId", id);
        j.put("compiled", JsonViews.compiledView(compiled));
        sendJson(ex, 201, j);
    }

    private void feedSession(HttpExchange ex, Session session, Map<String, Object> req) throws IOException {
        synchronized (session) {
            if (session.finished) {
                sendError(ex, 409, "session already finished");
                return;
            }
            List<Match> out = new ArrayList<>();
            Object chunk = req.get("chunk");
            if (chunk != null) {
                session.m.feed(asString(chunk, "chunk"), out);
            }
            Object bytesB64 = req.get("bytesBase64");
            if (bytesB64 != null) {
                session.m.feedBytes(java.util.Base64.getDecoder().decode(asString(bytesB64, "bytesBase64")), out);
            }
            Map<String, Object> j = new LinkedHashMap<>();
            j.put("emittedCount", out.size());
            j.put("matches", JsonViews.matchViews(out));
            sendJson(ex, 200, j);
        }
    }

    private void finishSession(HttpExchange ex, Session session, String id) throws IOException {
        synchronized (session) {
            if (session.finished) {
                sendError(ex, 409, "session already finished");
                return;
            }
            session.finished = true;
            List<Match> tail = session.m.finish();
            sessions.remove(id);
            Map<String, Object> j = new LinkedHashMap<>();
            j.put("emittedCount", tail.size());
            j.put("matches", JsonViews.matchViews(tail));
            sendJson(ex, 200, j);
        }
    }

    private void corpusGet(HttpExchange ex) throws IOException {
        Map<String, String> params = queryParams(ex);
        String profile = params.getOrDefault("profile", "dna");
        int textLength = intOrDefault(params, "textLength", 400);
        int patternCount = intOrDefault(params, "patternCount", 20);
        boolean includeText = Boolean.parseBoolean(params.getOrDefault("includeText", "true"));
        CorpusProfile cp = SyntheticCorpus.generate(profile, textLength, patternCount);
        sendJson(ex, 200, JsonViews.corpusView(cp, includeText));
    }

    private void corpusRun(HttpExchange ex, Map<String, Object> req) throws IOException {
        String profile = str(req, "profile", "dna");
        int textLength = intOrDefault(req, "textLength", 400);
        int patternCount = intOrDefault(req, "patternCount", 20);
        int chunkSize = intOrDefault(req, "chunkSize", 13);
        String unitRaw = str(req, "chunkUnit", "CODEPOINT");
        CorpusRunner.ChunkUnit unit;
        try {
            unit = CorpusRunner.ChunkUnit.valueOf(unitRaw.trim().toUpperCase());
        } catch (IllegalArgumentException e) {
            throw new IllegalArgumentException("chunkUnit must be CODEPOINT or UTF8_BYTE: " + unitRaw);
        }
        EmptyPatternPolicy policy = EmptyPatternPolicy.fromString(str(req, "emptyPatternPolicy", "MATCH_EVERY_POSITION"));

        CorpusProfile cp = SyntheticCorpus.generate(profile, textLength, patternCount);
        List<Pattern> patterns = CorpusRunner.patterns(cp, null);
        // 允许在合成模式之外追加用户模式（可含空模式以测试策略）。
        PatternsSpec extra = readPatternsOrEmpty(req);
        patterns.addAll(extra.patterns);
        Compiled compiled = Engine.compile(patterns, policy);
        CorpusRunner.RunResult result = CorpusRunner.run(compiled, cp.text(), unit, chunkSize);

        Map<String, Object> j = new LinkedHashMap<>();
        j.put("corpus", JsonViews.corpusView(cp, boolOrDefault(req, "includeText", false)));
        j.put("run", JsonViews.runView(result));
        j.put("compiled", JsonViews.compiledView(compiled));
        if (boolOrDefault(req, "includeMatches", false)) {
            j.put("matches", JsonViews.matchViews(result.matches()));
        }
        sendJson(ex, 200, j);
    }

    // ---- 请求解析辅助 ----

    private record PatternsSpec(List<Pattern> patterns, EmptyPatternPolicy policy) {
    }

    private static PatternsSpec readPatternsOrEmpty(Map<String, Object> req) {
        Object raw = req.get("patterns");
        if (raw == null) {
            return new PatternsSpec(new ArrayList<>(),
                    EmptyPatternPolicy.fromString(str(req, "emptyPatternPolicy", "MATCH_EVERY_POSITION")));
        }
        return readPatterns(req);
    }

    @SuppressWarnings("unchecked")
    private static PatternsSpec readPatterns(Map<String, Object> req) {
        EmptyPatternPolicy policy = EmptyPatternPolicy.fromString(str(req, "emptyPatternPolicy", "MATCH_EVERY_POSITION"));
        Object raw = req.get("patterns");
        List<Pattern> patterns = new ArrayList<>();
        if (raw instanceof List<?> list) {
            for (int i = 0; i < list.size(); i++) {
                Object item = list.get(i);
                if (item instanceof String s) {
                    patterns.add(Pattern.of("p" + i, s));
                } else if (item instanceof Map<?, ?> m) {
                    String literal = m.get("literal") == null ? null : String.valueOf(m.get("literal"));
                    if (literal == null) {
                        throw new JsonException("patterns[" + i + "].literal is required");
                    }
                    String id = m.get("id") == null ? "p" + i : String.valueOf(m.get("id"));
                    patterns.add(Pattern.of(id, literal));
                } else {
                    throw new JsonException("patterns[" + i + "] must be a string or {id,literal} object");
                }
            }
        } else if (raw != null) {
            throw new JsonException("patterns must be an array");
        }
        return new PatternsSpec(patterns, policy);
    }

    private static String str(Map<String, Object> req, String key, String dflt) {
        Object v = req.get(key);
        return v == null ? dflt : asString(v, key);
    }

    private static String asString(Object v, String key) {
        if (!(v instanceof String)) {
            throw new JsonException(key + " must be a string");
        }
        return (String) v;
    }

    private static int intOrDefault(Map<String, ?> req, String key, int dflt) {
        Object v = req.get(key);
        if (v == null) {
            return dflt;
        }
        if (v instanceof Number n) {
            return n.intValue();
        }
        try {
            return Integer.parseInt(String.valueOf(v));
        } catch (NumberFormatException e) {
            throw new JsonException(key + " must be an integer");
        }
    }

    private static boolean boolOrDefault(Map<String, ?> req, String key, boolean dflt) {
        Object v = req.get(key);
        return v == null ? dflt : Boolean.parseBoolean(String.valueOf(v));
    }

    private static Map<String, String> queryParams(HttpExchange ex) {
        Map<String, String> out = new LinkedHashMap<>();
        String q = ex.getRequestURI().getRawQuery();
        if (q == null) {
            return out;
        }
        for (String pair : q.split("&")) {
            int eq = pair.indexOf('=');
            String k = eq < 0 ? pair : pair.substring(0, eq);
            String v = eq < 0 ? "" : pair.substring(eq + 1);
            out.put(java.net.URLDecoder.decode(k, StandardCharsets.UTF_8),
                    java.net.URLDecoder.decode(v, StandardCharsets.UTF_8));
        }
        return out;
    }

    private String readBody(HttpExchange ex) throws IOException {
        try (InputStream is = ex.getRequestBody()) {
            return new String(is.readAllBytes(), StandardCharsets.UTF_8);
        }
    }

    private void sendJson(HttpExchange ex, int status, Object body) throws IOException {
        byte[] payload = JsonWriter.write(body).getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        ex.sendResponseHeaders(status, payload.length);
        try (OutputStream os = ex.getResponseBody()) {
            os.write(payload);
        }
    }

    private void sendError(HttpExchange ex, int status, String message) throws IOException {
        sendJson(ex, status, Map.of("error", message == null ? "" : message, "status", status));
    }

    // ---- main ----

    public static void main(String[] args) throws Exception {
        int port = 8080;
        String host = "127.0.0.1";
        if (args.length >= 1) {
            port = Integer.parseInt(args[0]);
        }
        if (args.length >= 2) {
            host = args[1];
        }
        HttpJsonServer srv = new HttpJsonServer(port, host);
        srv.start();
        System.out.println("streaming-aho-corasick listening on http://" + host + ":" + srv.getPort());
    }
}
