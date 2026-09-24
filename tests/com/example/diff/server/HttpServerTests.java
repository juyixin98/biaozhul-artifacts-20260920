package com.example.diff.server;

import com.example.diff.TestFramework;
import com.example.diff.json.Json;
import com.sun.net.httpserver.HttpServer;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** End-to-end tests against the real JDK HTTP server on an ephemeral port. */
public final class HttpServerTests {

    private HttpServerTests() {
    }

    private static HttpServer server;
    private static String base;
    private static final HttpClient CLIENT = HttpClient.newBuilder()
            .connectTimeout(Duration.ofSeconds(5))
            .build();

    public static void register(TestFramework tf) {
        tf.test("health endpoint", () -> withServer(HttpServerTests::health));
        tf.test("diff endpoint round trip + degradation flags", () -> withServer(HttpServerTests::diff));
        tf.test("diff on CRLF inputs keeps CRLF and appliedEqualsNew true", () -> withServer(HttpServerTests::crlfDiff));
        tf.test("budgeted diff reports degraded=true honestly", () -> withServer(HttpServerTests::degraded));
        tf.test("apply endpoint applies supplied script", () -> withServer(HttpServerTests::apply));
        tf.test("apply endpoint rejects malformed script with 422", () -> withServer(HttpServerTests::applyBad));
        tf.test("search endpoint hits synthetic corpus", () -> withServer(HttpServerTests::search));
        tf.test("corpus listing and document fetch", () -> withServer(HttpServerTests::corpus));
        tf.test("unknown route 404", () -> withServer(HttpServerTests::notFound));
        tf.test("bad JSON returns 400", () -> withServer(HttpServerTests::badJson));
    }

    private interface Checked {
        void run() throws Exception;
    }

    private static void withServer(Checked c) {
        try {
            if (server == null) {
                server = HttpServerMain.create(0);
                base = "http://localhost:" + server.getAddress().getPort();
            }
            c.run();
        } catch (Exception e) {
            throw new AssertionError(e);
        }
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> post(String path, Map<String, Object> body) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(base + path))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(Json.write(body), StandardCharsets.UTF_8))
                .build();
        HttpResponse<String> resp = CLIENT.send(req, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
        if (resp.statusCode() >= 500) {
            throw new AssertionError("server error " + resp.statusCode() + ": " + resp.body());
        }
        Map<String, Object> parsed;
        if (resp.statusCode() == 200 || resp.statusCode() == 422 || resp.statusCode() == 400) {
            parsed = new LinkedHashMap<>(Json.parseObject(resp.body()));
        } else {
            parsed = new LinkedHashMap<>();
        }
        parsed.put("__status", (long) resp.statusCode());
        return parsed;
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> get(String path) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(base + path)).GET().build();
        HttpResponse<String> resp = CLIENT.send(req, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
        Map<String, Object> parsed = Json.parseObject(resp.body());
        parsed.put("__status", (long) resp.statusCode());
        return parsed;
    }

    private static void health() throws Exception {
        Map<String, Object> r = get("/health");
        TestFramework.assertEquals(200L, r.get("__status"));
        TestFramework.assertEquals("ok", r.get("status"));
    }

    private static void diff() throws Exception {
        Map<String, Object> req = Map.of("old", "a\nb\n", "new", "a\nc\n");
        Map<String, Object> r = post("/api/diff", req);
        TestFramework.assertEquals(200L, r.get("__status"));
        TestFramework.assertEquals(false, r.get("degraded"));
        TestFramework.assertEquals(true, r.get("shortest"));
        TestFramework.assertEquals(true, r.get("appliedEqualsNew"));
        TestFramework.assertTrue(((List<Object>) r.get("edits")).size() > 0);
    }

    private static void crlfDiff() throws Exception {
        Map<String, Object> req = Map.of("old", "a\r\nb\r\n", "new", "a\r\nc\r\n");
        Map<String, Object> r = post("/api/diff", req);
        TestFramework.assertEquals(true, r.get("appliedEqualsNew"));
        String ud = (String) r.get("unifiedDiff");
        TestFramework.assertTrue(ud != null && ud.contains("-b\r\n"), "unified diff must carry CRLF text");
    }

    private static void degraded() throws Exception {
        Map<String, Object> req = new LinkedHashMap<>();
        req.put("old", "p\nq\n");
        req.put("new", "r\ns\n");
        req.put("maxEditDistance", 1L); // true distance is 4
        Map<String, Object> r = post("/api/diff", req);
        TestFramework.assertEquals(true, r.get("degraded"), "must honestly report degradation");
        TestFramework.assertEquals(false, r.get("shortest"));
        TestFramework.assertTrue(String.valueOf(r.get("degradeReason")).contains("maxEditDistance"));
        TestFramework.assertEquals(true, r.get("appliedEqualsNew"),
                "degraded scripts still apply exactly");
    }

    private static void apply() throws Exception {
        // First get an edit list, then round-trip it through /api/apply.
        Map<String, Object> diffReq = Map.of("old", "x\ny\n", "new", "x\nz\n");
        Map<String, Object> dr = post("/api/diff", diffReq);
        Map<String, Object> applyReq = Map.of("old", "x\ny\n", "edits", dr.get("edits"));
        Map<String, Object> ar = post("/api/apply", applyReq);
        TestFramework.assertEquals(200L, ar.get("__status"));
        TestFramework.assertEquals(true, ar.get("ok"));
        TestFramework.assertEquals("x\nz\n", ar.get("text"));
    }

    private static void applyBad() throws Exception {
        Map<String, Object> badEdit = new LinkedHashMap<>();
        badEdit.put("kind", "equal");
        badEdit.put("oldStart", 0);
        badEdit.put("oldEnd", 1);
        badEdit.put("newStart", 0);
        badEdit.put("newEnd", 1);
        badEdit.put("oldLines", List.of("nope\n"));
        badEdit.put("newLines", List.of("nope\n"));
        Map<String, Object> r = post("/api/apply", Map.of("old", "a\n", "edits", List.of(badEdit)));
        TestFramework.assertEquals(422L, r.get("__status"));
        TestFramework.assertEquals(false, r.get("ok"));
        TestFramework.assertTrue(r.get("error") != null);
    }

    private static void search() throws Exception {
        Map<String, Object> r = post("/api/search", Map.of("query", "compilation", "limit", 5));
        TestFramework.assertEquals(200L, r.get("__status"));
        TestFramework.assertTrue(((Number) r.get("count")).intValue() >= 2);
    }

    private static void corpus() throws Exception {
        Map<String, Object> list = get("/api/corpus");
        TestFramework.assertEquals(200L, list.get("__status"));
        @SuppressWarnings("unchecked")
        List<Object> docs = (List<Object>) list.get("documents");
        TestFramework.assertTrue(docs.size() >= 9, "synthetic corpus should list docs");

        Map<String, Object> doc = get("/api/corpus/log-002");
        TestFramework.assertEquals(200L, doc.get("__status"));
        TestFramework.assertEquals(true, doc.get("crlf"), "log-002 is the CRLF document");

        Map<String, Object> missing = get("/api/corpus/nope");
        TestFramework.assertEquals(404L, missing.get("__status"));
    }

    private static void notFound() throws Exception {
        Map<String, Object> r = get("/nope");
        TestFramework.assertEquals(404L, r.get("__status"));
    }

    private static void badJson() throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(base + "/api/diff"))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString("{not json", StandardCharsets.UTF_8))
                .build();
        HttpResponse<String> resp = CLIENT.send(req, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
        TestFramework.assertEquals(400, resp.statusCode());
    }
}
