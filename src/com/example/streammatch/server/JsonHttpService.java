package com.example.streammatch.server;

import com.example.streammatch.AhoCorasick;
import com.example.streammatch.CorpusGenerator;
import com.example.streammatch.EmptyPatternPolicy;
import com.example.streammatch.Match;
import com.example.streammatch.NaiveMatcher;
import com.example.streammatch.StreamMatcher;
import com.example.streammatch.json.Json;
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

/**
 * 纯后端 JSON 服务（JDK 内置 {@link HttpServer}，无任何第三方依赖）。
 *
 * <h3>路由</h3>
 * <ul>
 *   <li>{@code GET  /health}                 —— 健康检查；</li>
 *   <li>{@code GET  /corpora}                —— 列出内置合成语料；</li>
 *   <li>{@code POST /corpus?name=&seed=}     —— 生成/查看一份合成语料；</li>
 *   <li>{@code POST /match}                  —— 一次性匹配（text 或 corpusName）；</li>
 *   <li>{@code POST /match/stream}           —— 服务端按码点切块后流式匹配，
 *       逐块返回 hits 与全局位置，并与朴素匹配/一次性匹配核对。</li>
 * </ul>
 *
 * <p>匹配业务逻辑在静态方法中，与 HTTP 解耦，测试可直接调用。</p>
 */
public final class JsonHttpService {

    private JsonHttpService() {
    }

    /** 解析后的模式集合：按外部顺序的原文列表 + 每个模式的外部 ID（可显式指定）。 */
    public record PatternSpec(List<String> patterns, List<Integer> externalIds) {
    }

    // ------------------------------------------------------------------
    // 业务逻辑（与 HTTP 无关，便于测试）
    // ------------------------------------------------------------------

    /**
     * 解析 patterns 字段：元素可以是字符串，或 {@code {"id":1,"pattern":"he"}} 对象。
     * 缺省 id 时按数组下标编号；显式 id 允许重复/不连续。
     */
    public static PatternSpec parsePatterns(Object raw) {
        if (!(raw instanceof List<?> list)) {
            throw new BadRequestException("patterns 必须是数组");
        }
        List<String> patterns = new ArrayList<>(list.size());
        List<Integer> ids = new ArrayList<>(list.size());
        int auto = 0;
        for (Object item : list) {
            if (item instanceof String s) {
                patterns.add(s);
                ids.add(auto);
            } else if (item instanceof Map<?, ?> m) {
                Object p = m.get("pattern");
                if (!(p instanceof String ps)) {
                    throw new BadRequestException("patterns 对象元素必须含字符串字段 pattern");
                }
                Object idObj = m.get("id");
                int id;
                if (idObj instanceof Number n) {
                    id = n.intValue();
                } else if (idObj == null) {
                    id = auto;
                } else {
                    throw new BadRequestException("patterns 元素 id 必须是整数");
                }
                patterns.add(ps);
                ids.add(id);
            } else {
                throw new BadRequestException("patterns 元素必须是字符串或 {id, pattern} 对象");
            }
            auto++;
        }
        return new PatternSpec(patterns, ids);
    }

    static EmptyPatternPolicy parsePolicy(String p) {
        if (p == null || p.isEmpty()) {
            return EmptyPatternPolicy.SKIP;
        }
        try {
            return EmptyPatternPolicy.valueOf(p.toUpperCase());
        } catch (IllegalArgumentException e) {
            throw new BadRequestException("emptyPolicy 只能是 SKIP/BEFORE/AFTER，得到: " + p);
        }
    }

    static Map<String, Object> matchToJson(Match m, List<Integer> externalIds) {
        Map<String, Object> j = new LinkedHashMap<>();
        j.put("patternId", externalIds.get(m.patternId()));
        j.put("start", m.start());
        j.put("end", m.end());
        j.put("length", m.end() - m.start());
        j.put("pattern", m.pattern());
        return j;
    }

    static List<Match> sorted(List<Match> ms) {
        ms.sort(Match.canonicalOrder());
        return ms;
    }

    /** 把内部模式 ID 映射为外部 ID 后做规范化排序。 */
    static List<Map<String, Object>> project(List<Match> ms, List<Integer> externalIds) {
        List<Match> copy = sorted(new ArrayList<>(ms));
        List<Map<String, Object>> out = new ArrayList<>(copy.size());
        for (Match m : copy) {
            out.add(matchToJson(m, externalIds));
        }
        return out;
    }

    /**
     * 一次性匹配。
     *
     * @param request 已解析的请求对象
     */
    public static Map<String, Object> handleMatch(Map<String, Object> request) {
        PatternSpec spec = parsePatterns(request.get("patterns"));
        EmptyPatternPolicy policy = parsePolicy(Json.getStringOrDefault(request, "emptyPolicy", null));

        String text;
        String source;
        Object corpusName = request.get("corpusName");
        if (request.get("text") instanceof String t) {
            if (corpusName != null) {
                throw new BadRequestException("text 与 corpusName 只能提供一个");
            }
            text = t;
            source = "inline";
        } else if (corpusName instanceof String cn) {
            int seed = Json.getIntOrDefault(request, "seed", 42);
            CorpusGenerator.Corpus corpus = CorpusGenerator.generate(cn, seed);
            text = corpus.text();
            source = "corpus:" + cn + "?seed=" + seed;
        } else {
            throw new BadRequestException("必须提供 text（字符串）或 corpusName（" + CorpusGenerator.names() + "）");
        }

        AhoCorasick ac = new AhoCorasick(spec.patterns(), policy);
        List<Match> acMatches = ac.matchAll(text);

        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("ok", true);
        resp.put("source", source);
        resp.put("emptyPolicy", policy.name());
        resp.put("textCodePoints", text.codePointCount(0, text.length()));
        resp.put("textChars", text.length());
        resp.put("patternCount", spec.patterns().size());
        resp.put("matchCount", acMatches.size());
        resp.put("matches", project(acMatches, spec.externalIds()));

        boolean compare = Json.getBoolOrDefault(request, "compareWithNaive", true);
        if (compare) {
            List<Match> naive = NaiveMatcher.match(text, spec.patterns(), policy);
            boolean equal = canonicalEquals(acMatches, naive);
            resp.put("naiveMatchCount", naive.size());
            resp.put("matchesNaive", equal);
            if (!equal) {
                resp.put("firstDifference", firstDifference(
                        sorted(new ArrayList<>(acMatches)), naive));
            }
        }
        return resp;
    }

    /**
     * 流式匹配：把文本按“码点数”切成若干块（可选按 UTF-8 字节切，刻意切断多字节字符），
     * 逐块喂给 {@link StreamMatcher}，返回每块的命中与全局累积结果，并核对一致性。
     */
    public static Map<String, Object> handleStream(Map<String, Object> request) {
        PatternSpec spec = parsePatterns(request.get("patterns"));
        EmptyPatternPolicy policy = parsePolicy(Json.getStringOrDefault(request, "emptyPolicy", null));

        String text;
        if (request.get("text") instanceof String t) {
            text = t;
        } else if (request.get("corpusName") instanceof String cn) {
            int seed = Json.getIntOrDefault(request, "seed", 42);
            text = CorpusGenerator.generate(cn, seed).text();
        } else {
            throw new BadRequestException("必须提供 text 或 corpusName");
        }

        int chunkSize = Json.getIntOrDefault(request, "chunkCodePoints", 7);
        if (chunkSize <= 0) {
            throw new BadRequestException("chunkCodePoints 必须为正整数");
        }
        // byteMode 下按 UTF-8 字节数切（会切断多字节序列/代理对），用于验证跨块边界
        boolean byteMode = Json.getBoolOrDefault(request, "byteMode", false);
        int byteChunk = Json.getIntOrDefault(request, "chunkBytes", 3);

        AhoCorasick ac = new AhoCorasick(spec.patterns(), policy);
        StreamMatcher sm = new StreamMatcher(ac);

        List<Map<String, Object>> chunks = new ArrayList<>();
        List<Match> gathered = new ArrayList<>();

        if (byteMode) {
            if (byteChunk <= 0) {
                throw new BadRequestException("chunkBytes 必须为正整数");
            }
            byte[] all = text.getBytes(StandardCharsets.UTF_8);
            int totalChunks = (all.length + byteChunk - 1) / byteChunk;
            int index = 0;
            for (int off = 0; off < all.length; off += byteChunk) {
                int len = Math.min(byteChunk, all.length - off);
                sm.feedChunkBytes(all, off, len, true);
                List<Match> produced = sm.drainMatches();
                gathered.addAll(produced);
                chunks.add(chunkJson(index, totalChunks,
                        "byte[" + off + "," + (off + len) + ")", produced, spec.externalIds()));
                index++;
            }
            List<Match> tail = sm.finish();
            gathered.addAll(tail);
            chunks.add(chunkJson(index, totalChunks, "finish", tail, spec.externalIds()));
        } else {
            int total = text.codePointCount(0, text.length());
            int totalChunks = (total + chunkSize - 1) / chunkSize + 1;
            int index = 0;
            int jStart = 0; // char 下标随码点块增量推进，避免每块 O(n) 重算（整体 O(n)）
            for (int cpStart = 0; cpStart < total; cpStart += chunkSize) {
                int cpEnd = Math.min(total, cpStart + chunkSize);
                int jEnd = text.offsetByCodePoints(jStart, cpEnd - cpStart);
                String piece = text.substring(jStart, jEnd);
                sm.feedChunk(piece);
                List<Match> produced = sm.drainMatches();
                gathered.addAll(produced);
                chunks.add(chunkJson(index, totalChunks,
                        "codePoint[" + cpStart + "," + cpEnd + ")",
                        produced, spec.externalIds()));
                index++;
                jStart = jEnd;
            }
            List<Match> tail = sm.finish();
            gathered.addAll(tail);
            chunks.add(chunkJson(index, totalChunks, "finish", tail, spec.externalIds()));
        }

        // 参照 1：一次性 AC 匹配
        List<Match> oneShot = ac.matchAll(text);
        // 参照 2：朴素匹配
        List<Match> naive = NaiveMatcher.match(text, spec.patterns(), policy);

        boolean eqOneShot = canonicalEquals(gathered, oneShot);
        boolean eqNaive = canonicalEquals(gathered, naive);

        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("ok", true);
        resp.put("mode", byteMode ? "utf8-bytes(" + byteChunk + ")" : "codepoints(" + chunkSize + ")");
        resp.put("emptyPolicy", policy.name());
        resp.put("textCodePoints", text.codePointCount(0, text.length()));
        resp.put("streamedMatchCount", gathered.size());
        resp.put("oneShotMatchCount", oneShot.size());
        resp.put("naiveMatchCount", naive.size());
        resp.put("matchesStream", eqOneShot);
        resp.put("matchesNaive", eqNaive);
        resp.put("chunks", chunks);
        resp.put("matches", project(gathered, spec.externalIds()));
        return resp;
    }

    private static Map<String, Object> chunkJson(int index, int total, String span,
                                                 List<Match> produced,
                                                 List<Integer> externalIds) {
        Map<String, Object> j = new LinkedHashMap<>();
        j.put("index", index);
        j.put("span", span);
        j.put("hitCount", produced.size());
        j.put("hits", project(produced, externalIds));
        return j;
    }

    /** 规范化比较两个命中集合（内部模式 ID）。 */
    public static boolean canonicalEquals(List<Match> a, List<Match> b) {
        List<Match> x = sorted(new ArrayList<>(a));
        List<Match> y = sorted(new ArrayList<>(b));
        return x.equals(y);
    }

    static Map<String, Object> firstDifference(List<Match> a, List<Match> b) {
        int n = Math.min(a.size(), b.size());
        for (int i = 0; i < n; i++) {
            if (!a.get(i).equals(b.get(i))) {
                Map<String, Object> d = new LinkedHashMap<>();
                d.put("index", i);
                d.put("stream", simpleMatch(a.get(i)));
                d.put("naive", simpleMatch(b.get(i)));
                return d;
            }
        }
        Map<String, Object> d = new LinkedHashMap<>();
        d.put("index", n);
        d.put("streamCount", a.size());
        d.put("naiveCount", b.size());
        return d;
    }

    private static Map<String, Object> simpleMatch(Match m) {
        Map<String, Object> j = new LinkedHashMap<>();
        j.put("patternId", m.patternId());
        j.put("start", m.start());
        j.put("end", m.end());
        return j;
    }

    // ------------------------------------------------------------------
    // HTTP 封装
    // ------------------------------------------------------------------

    public static final class BadRequestException extends RuntimeException {
        @SuppressWarnings("unused")
        private static final long serialVersionUID = 1L;

        public BadRequestException(String msg) {
            super(msg);
        }
    }

    /**
     * 创建并启动 HTTP 服务（4 线程守护线程池，不会阻止 JVM 退出），返回服务实例。
     * 测试可直接 {@code server.stop(0)}；Main 中由进程生命周期持有。
     */
    public static HttpServer startServer(int port) throws IOException {
        HttpServer server = HttpServer.create(new InetSocketAddress(port), 0);
        java.util.concurrent.ThreadFactory daemonFactory = r -> {
            Thread t = new Thread(r, "streammatch-http");
            t.setDaemon(true);
            return t;
        };
        server.createContext("/health", JsonHttpService::healthOnlyGet);
        server.createContext("/corpora", JsonHttpService::corpora);
        server.createContext("/corpus", ex -> dispatch(ex, "corpus"));
        server.createContext("/match/stream", ex -> dispatch(ex, "stream"));
        server.createContext("/match", ex -> dispatch(ex, "match"));
        server.setExecutor(java.util.concurrent.Executors.newFixedThreadPool(4, daemonFactory));
        server.start();
        return server;
    }

    private static void healthOnlyGet(HttpExchange ex) throws IOException {
        if (!"GET".equalsIgnoreCase(ex.getRequestMethod())) {
            writeError(ex, 405, "只支持 GET");
            return;
        }
        health(ex);
    }

    private static void health(HttpExchange ex) throws IOException {
        Map<String, Object> body = Map.of("ok", true, "service", "stream-multimatch", "version", "1.0");
        writeJson(ex, 200, body);
    }

    private static void corpora(HttpExchange ex) throws IOException {
        if (!"GET".equalsIgnoreCase(ex.getRequestMethod())) {
            writeError(ex, 405, "只支持 GET");
            return;
        }
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("ok", true);
        body.put("corpora", CorpusGenerator.names());
        writeJson(ex, 200, body);
    }

    private static void dispatch(HttpExchange ex, String kind) {
        try {
            if (!"POST".equalsIgnoreCase(ex.getRequestMethod())) {
                writeError(ex, 405, "只支持 POST");
                return;
            }
            String reqText = new String(ex.getRequestBody().readAllBytes(), StandardCharsets.UTF_8);
            Map<String, Object> req = reqText.isBlank() ? new LinkedHashMap<>() : Json.parseObject(reqText);

            // /corpus 也支持 query 参数形式 POST 空体
            Map<String, Object> resp = switch (kind) {
                case "corpus" -> handleCorpus(ex, req);
                case "match" -> handleMatch(req);
                case "stream" -> handleStream(req);
                default -> throw new IllegalStateException(kind);
            };
            writeJson(ex, 200, resp);
        } catch (BadRequestException | IllegalArgumentException e) {
            writeError(ex, 400, e.getMessage());
        } catch (Exception e) {
            writeError(ex, 500, "内部错误: " + e);
        }
    }

    private static Map<String, Object> handleCorpus(HttpExchange ex, Map<String, Object> req) {
        String name = queryParam(ex, "name");
        if (name == null) {
            name = Json.getStringOrDefault(req, "name", null);
        }
        long seed = Json.getIntOrDefault(req, "seed", 42);
        String seedParam = queryParam(ex, "seed");
        if (seedParam != null) {
            seed = Long.parseLong(seedParam);
        }
        CorpusGenerator.Corpus c = CorpusGenerator.generate(name, seed);
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("ok", true);
        resp.put("name", c.name());
        resp.put("seed", seed);
        resp.put("patternCount", c.patterns().size());
        resp.put("patterns", c.patterns());
        resp.put("textCodePoints", c.text().codePointCount(0, c.text().length()));
        boolean includeText = Json.getBoolOrDefault(req, "includeText", true);
        if (includeText) {
            resp.put("text", c.text());
        }
        return resp;
    }

    private static String queryParam(HttpExchange ex, String key) {
        String q = ex.getRequestURI().getRawQuery();
        if (q == null) {
            return null;
        }
        for (String pair : q.split("&")) {
            int eq = pair.indexOf('=');
            if (eq > 0 && pair.substring(0, eq).equals(key)) {
                return java.net.URLDecoder.decode(pair.substring(eq + 1), StandardCharsets.UTF_8);
            }
        }
        return null;
    }

    private static void writeJson(HttpExchange ex, int status, Object body) throws IOException {
        byte[] payload = Json.write(body).getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().add("Content-Type", "application/json; charset=utf-8");
        ex.sendResponseHeaders(status, payload.length);
        try (OutputStream os = ex.getResponseBody()) {
            os.write(payload);
        }
    }

    private static void writeError(HttpExchange ex, int status, String message) {
        try {
            Map<String, Object> body = new LinkedHashMap<>();
            body.put("ok", false);
            body.put("error", message);
            writeJson(ex, status, body);
        } catch (IOException ignored) {
            // 连接已断开时无需再处理
        }
    }
}
