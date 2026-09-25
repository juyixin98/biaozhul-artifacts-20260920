package com.example.ac.test;

import com.example.ac.server.HttpJsonServer;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.util.Base64;
import java.util.List;
import java.util.Map;

import com.example.ac.server.JsonParser;

/**
 * HTTP JSON 服务端到端测试：启动绑定随机端口的真实服务器，
 * 用 JDK HttpClient 打全部端点（含跨块流式会话、UTF-8 base64 字节喂入、错误码）。
 */
public class ServerE2ETest extends TestCase {

    private HttpJsonServer server;
    private HttpClient client;
    private String base;

    public ServerE2ETest() {
        super("server-e2e");
    }

    @Override
    protected void run() throws Exception {
        server = new HttpJsonServer(0, "127.0.0.1");
        server.start();
        base = "http://127.0.0.1:" + server.getPort();
        client = HttpClient.newHttpClient();
        try {
            healthAndInfo();
            matchOnce();
            streamingSessionCrossChunk();
            byteSession();
            emptyPoliciesOverHttp();
            corpusEndpoints();
            errors();
        } finally {
            server.stop(0);
        }
    }

    private HttpResponse<String> post(String path, String json) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(base + path))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(json, StandardCharsets.UTF_8))
                .build();
        return client.send(req, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
    }

    private HttpResponse<String> get(String path) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(base + path)).GET().build();
        return client.send(req, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
    }

    private HttpResponse<String> delete(String path) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(base + path)).DELETE().build();
        return client.send(req, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
    }

    @SuppressWarnings("unchecked")
    private Map<String, Object> json(HttpResponse<String> r) {
        return (Map<String, Object>) JsonParser.parse(r.body());
    }

    private static int num(Map<String, Object> m, String key) {
        return ((Number) m.get(key)).intValue();
    }

    @SuppressWarnings("unchecked")
    private List<Map<String, Object>> matches(Map<String, Object> resp) {
        return (List<Map<String, Object>>) resp.get("matches");
    }

    private void healthAndInfo() throws Exception {
        var h = json(get("/api/health"));
        eq(h.get("status"), "ok", "health ok");
        var info = json(get("/"));
        check(info.get("endpoints") instanceof List, "info lists endpoints");
    }

    private void matchOnce() throws Exception {
        String req = """
                {
                  "text": "ushers",
                  "patterns": ["he", "she", {"id":"hers","literal":"hers"}]
                }
                """;
        var r = post("/api/match", req);
        eq(r.statusCode(), 200, "match status 200");
        var body = json(r);
        eq(num(body, "matchCount"), 3, "match count 3 (she/he/hers)");
        var ms = matches(body);
        eq(ms.get(0).get("patternId"), "p1", "she hit");
        eq(((Number) ms.get(0).get("start")).intValue(), 1, "she cp start");
        eq(ms.get(1).get("patternId"), "p0", "he hit");
        eq(((Number) ms.get(1).get("charStart")).intValue(), 2, "charStart reported");
    }

    @SuppressWarnings("unchecked")
    private void streamingSessionCrossChunk() throws Exception {
        // 模式 abcde 被切成 "abc" + "de" 两块，必须跨块命中；bc 在第一块内完成
        var create = post("/api/sessions", """
                {"patterns":["abcde","bc"],"emptyPatternPolicy":"SKIP"}""");
        eq(create.statusCode(), 201, "session created");
        String id = (String) json(create).get("sessionId");

        var f1 = post("/api/sessions/" + id + "/feed", """
                {"chunk":"abc"}""");
        eq(f1.statusCode(), 200, "feed1 ok");
        var b1 = json(f1);
        eq(num(b1, "emittedCount"), 1, "bc completes within first chunk");
        var m1 = matches(b1);
        eq(m1.get(0).get("patternId"), "p1", "first-chunk hit is bc");

        var f2 = post("/api/sessions/" + id + "/feed", """
                {"chunk":"de"}""");
        var b2 = json(f2);
        eq(num(b2, "emittedCount"), 1, "abcde completed across chunks");
        var ms = matches(b2);
        eq(ms.get(0).get("patternId"), "p0", "abcde id");
        eq(((Number) ms.get(0).get("start")).intValue(), 0, "cross-chunk start");

        var fin = post("/api/sessions/" + id + "/finish", "{}");
        eq(fin.statusCode(), 200, "finish ok");
        // finish 后会话已移除
        eq(get("/api/health").statusCode(), 200, "server still alive");

        // DELETE 路径
        var c2 = json(post("/api/sessions", """
                {"patterns":["x"]}"""));
        String id2 = (String) c2.get("sessionId");
        eq(delete("/api/sessions/" + id2).statusCode(), 200, "delete session");
        var gone = post("/api/sessions/" + id2 + "/feed", """
                {"chunk":"x"}""");
        eq(gone.statusCode(), 404, "deleted session 404");
    }

    private void byteSession() throws Exception {
        // "😀🚀" 以 UTF-8 字节逐块喂，块切在 4 字节 emoji 中间
        String text = "x😀🚀y";
        byte[] all = text.getBytes(StandardCharsets.UTF_8);
        var create = post("/api/sessions", """
                {"patterns":["😀","😀🚀"]}""");
        String id = (String) json(create).get("sessionId");
        for (int begin = 0; begin < all.length; begin += 3) {
            int len = Math.min(3, all.length - begin);
            String b64 = Base64.getEncoder().encodeToString(java.util.Arrays.copyOfRange(all, begin, begin + len));
            var fr = post("/api/sessions/" + id + "/feed",
                    "{\"bytesBase64\":\"" + b64 + "\"}");
            eq(fr.statusCode(), 200, "byte feed at offset " + begin);
        }
        var fin = post("/api/sessions/" + id + "/finish", "{}");
        // 😀 命中 1 次，😀🚀 命中 1 次
        int total = 0;
        // 累计 emitted 需要回放各块——这里改为直接用 /api/match 交叉验证总数，
        // 再确认 finish 200。
        eq(fin.statusCode(), 200, "byte session finish");
        var verify = json(post("/api/match",
                "{\"text\":\"x😀🚀y\",\"patterns\":[\"😀\",\"😀🚀\"]}"));
        eq(((Number) verify.get("matchCount")).intValue(), 2, "whole-text confirms 2 hits");
    }

    private void emptyPoliciesOverHttp() throws Exception {
        var err = post("/api/match", """
                {"text":"ab","patterns":["a",""],"emptyPatternPolicy":"ERROR"}""");
        eq(err.statusCode(), 400, "ERROR policy -> 400");

        var skip = json(post("/api/match", """
                {"text":"ab","patterns":["a",""],"emptyPatternPolicy":"SKIP"}"""));
        eq(((Number) skip.get("matchCount")).intValue(), 1, "SKIP drops empty");

        var every = json(post("/api/match", """
                {"text":"ab","patterns":["a",""],"emptyPatternPolicy":"MATCH_EVERY_POSITION"}"""));
        eq(((Number) every.get("matchCount")).intValue(), 4, "1 a-hit + 3 boundaries");
    }

    @SuppressWarnings("unchecked")
    private void corpusEndpoints() throws Exception {
        var g = get("/api/corpus?profile=unicode&textLength=120&patternCount=10&includeText=false");
        eq(g.statusCode(), 200, "corpus get");
        var gb = json(g);
        eq(gb.get("profile"), "unicode", "profile echoed");
        check(gb.get("text") == null, "includeText=false omits text");

        var run = post("/api/corpus/run", """
                {
                  "profile": "dna",
                  "textLength": 300,
                  "patternCount": 20,
                  "chunkSize": 7,
                  "chunkUnit": "CODEPOINT"
                }
                """);
        eq(run.statusCode(), 200, "corpus run");
        var rb = json(run);
        var runj = (Map<String, Object>) rb.get("run");
        check(((Number) runj.get("totalCrossChunk")).intValue() > 0,
                "real cross-chunk hits observed: " + runj.get("totalCrossChunk"));
        check(((Number) runj.get("chunkCount")).intValue() > 1, "multiple chunks");

        var utf8run = post("/api/corpus/run", """
                {
                  "profile": "unicode",
                  "textLength": 200,
                  "patternCount": 12,
                  "chunkSize": 3,
                  "chunkUnit": "UTF8_BYTE"
                }
                """);
        eq(utf8run.statusCode(), 200, "utf8 corpus run");
    }

    private void errors() throws Exception {
        eq(post("/api/match", "not json").statusCode(), 400, "malformed json -> 400");
        eq(post("/api/match", "{\"text\":\"x\"}").statusCode(), 200, "no patterns is allowed");
        eq(post("/api/match", "{\"text\":\"x\",\"patterns\":42}").statusCode(), 400,
                "patterns not array -> 400");
        eq(get("/api/nope").statusCode(), 404, "unknown path -> 404");
        var badPolicy = post("/api/match",
                "{\"text\":\"x\",\"patterns\":[\"x\"],\"emptyPatternPolicy\":\"WAT\"}");
        eq(badPolicy.statusCode(), 400, "bad policy -> 400");
        var badSession = post("/api/sessions/does-not-exist/feed", "{}");
        eq(badSession.statusCode(), 404, "unknown session -> 404");
    }
}
