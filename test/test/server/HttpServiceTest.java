package test.server;

import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import approx.json.Json;
import approx.server.HttpEventServer;
import approx.time.ManualEnvironment;
import test.Asserts;
import test.TestRunner;

public final class HttpServiceTest {

    private HttpServiceTest() {
    }

    private static final class Session {
        HttpEventServer server;
        ManualEnvironment env;
        HttpClient client;
        String base;

        void start() throws IOException {
            env = new ManualEnvironment(0L);
            server = HttpEventServer.start(0, env, env, true);
            server.setTimeAdvancer(env::advance);
            client = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(5)).build();
            base = "http://localhost:" + server.port();
        }

        void stop() {
            server.close();
        }

        @SuppressWarnings("unchecked")
        Map<String, Object> resp(HttpResponse<String> r) {
            return (Map<String, Object>) Json.parse(r.body());
        }

        HttpResponse<String> request(String method, String path, Object body)
                throws IOException, InterruptedException {
            HttpRequest.Builder b = HttpRequest.newBuilder(URI.create(base + path));
            if (body != null) {
                b.header("Content-Type", "application/json")
                        .method(method, HttpRequest.BodyPublishers.ofString(
                                Json.write(body), StandardCharsets.UTF_8));
            } else {
                b.method(method, HttpRequest.BodyPublishers.noBody());
            }
            return client.send(b.build(), HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
        }

        HttpResponse<String> post(String path, Object body) throws Exception {
            return request("POST", path, body);
        }

        HttpResponse<String> get(String path) throws Exception {
            return request("GET", path, null);
        }
    }

    public static void register(TestRunner r) {
        r.add("http: full lifecycle: create, events, topk, query, windows, delete",
                HttpServiceTest::testLifecycle);
        r.add("http: point query declares error upper bound, not candidate coverage",
                HttpServiceTest::testErrorContract);
        r.add("http: virtual clock rotates windows over the API", HttpServiceTest::testWindowRotation);
        r.add("http: late events reported as droppedLate", HttpServiceTest::testLateEvents);
        r.add("http: merge accepts compatible snapshot", HttpServiceTest::testMergeCompatible);
        r.add("http: merge rejects different seed (409)", HttpServiceTest::testMergeRejectSeed);
        r.add("http: merge rejects different width (409)", HttpServiceTest::testMergeRejectWidth);
        r.add("http: merge rejects different depth (409)", HttpServiceTest::testMergeRejectDepth);
        r.add("http: malformed JSON -> 400, unknown engine -> 404", HttpServiceTest::testErrors);
        r.add("http: engine created from epsilon/delta target", HttpServiceTest::testEpsilonConfig);
        r.add("http: advance-time refused in non-manual mode", HttpServiceTest::testAdvanceRefused);
    }

    private static Map<String, Object> engineSpec(long windowMillis) {
        Map<String, Object> spec = new LinkedHashMap<>();
        spec.put("id", "svc");
        spec.put("width", 256);
        spec.put("depth", 5);
        spec.put("seed", 42);
        spec.put("candidateCapacity", 5);
        spec.put("windowMillis", windowMillis);
        spec.put("trackExact", true);
        return spec;
    }

    private static void testLifecycle() throws Exception {
        Session s = new Session();
        s.start();
        try {
            var health = s.get("/healthz");
            Asserts.assertEquals(200, health.statusCode(), "healthz 200");

            var created = s.post("/v1/engines", engineSpec(0L));
            Asserts.assertEquals(201, created.statusCode(), "engine created");

            Map<String, Object> batch = new LinkedHashMap<>();
            batch.put("events", List.of(
                    Map.of("item", "apple", "count", 50),
                    Map.of("item", "pear", "count", 30),
                    "banana",
                    Map.of("item", "kiwi", "count", 5)));
            var added = s.post("/v1/engines/svc/events", batch);
            Asserts.assertEquals(200, added.statusCode(), "events accepted");
            Asserts.assertEquals(86L, ((Number) s.resp(added).get("accepted")).longValue(),
                    "accepted 50+30+1+5");

            var topk = s.get("/v1/engines/svc/topk?k=3");
            @SuppressWarnings("unchecked")
            List<Object> items = (List<Object>) s.resp(topk).get("items");
            Asserts.assertEquals("apple", ((Map<?, ?>) items.get(0)).get("item"), "apple first");
            Asserts.assertTrue(String.valueOf(s.resp(topk).get("coverageGuarantee")).startsWith("none"),
                    "coverage non-guarantee declared");

            var item = s.get("/v1/engines/svc/items/apple");
            Asserts.assertEquals(50L, ((Number) s.resp(item).get("estimate")).longValue(), "apple estimate");
            Asserts.assertEquals(50L, ((Number) s.resp(item).get("trueCount")).longValue(), "apple true");

            var flush = s.post("/v1/engines/svc/flush", Map.of());
            Asserts.assertEquals(200, flush.statusCode(), "flush 200");
            var windows = s.get("/v1/engines/svc/windows");
            Asserts.assertEquals(1, ((List<?>) s.resp(windows).get("windows")).size(), "one window");

            var deleted = s.request("DELETE", "/v1/engines/svc", null);
            Asserts.assertEquals(200, deleted.statusCode(), "deleted");
            Asserts.assertEquals(404, s.get("/v1/engines/svc").statusCode(), "gone after delete");
        } finally {
            s.stop();
        }
    }

    private static void testErrorContract() throws Exception {
        Session s = new Session();
        s.start();
        try {
            s.post("/v1/engines", engineSpec(0L));
            s.post("/v1/engines/svc/events", Map.of("events", List.of(Map.of("item", "x", "count", 100))));
            var q = s.get("/v1/engines/svc/items/x");
            Map<String, Object> body = s.resp(q);
            Asserts.assertTrue(body.containsKey("errorUpperBound"), "bound present");
            Asserts.assertTrue(body.containsKey("epsilon") && body.containsKey("delta"),
                    "epsilon/delta present");
            var topk = s.get("/v1/engines/svc/topk?k=1");
            // The response explicitly refuses to promise top-K coverage.
            String guarantee = String.valueOf(s.resp(topk).get("coverageGuarantee"));
            Asserts.assertTrue(guarantee.contains("may have been evicted"), "non-coverage stated");
        } finally {
            s.stop();
        }
    }

    private static void testWindowRotation() throws Exception {
        Session s = new Session();
        s.start();
        try {
            s.post("/v1/engines", engineSpec(1000L));
            s.post("/v1/engines/svc/events", Map.of("events", List.of(Map.of("item", "a", "timestampMillis", 100))));
            s.post("/v1/engines/svc/events", Map.of("events", List.of(Map.of("item", "b", "timestampMillis", 1500))));

            var advanced = s.post("/v1/admin/advance-time", Map.of("millis", 3000));
            Asserts.assertEquals(200, advanced.statusCode(), "time advanced");
            Asserts.assertEquals(3000L, ((Number) s.resp(advanced).get("nowMillis")).longValue(),
                    "virtual clock at 3000");

            var windows = s.get("/v1/engines/svc/windows");
            @SuppressWarnings("unchecked")
            List<Object> list = (List<Object>) s.resp(windows).get("windows");
            Asserts.assertEquals(3, list.size(), "windows 0..2 closed after advancing to 3000");

            var w0 = s.get("/v1/engines/svc/windows/0");
            @SuppressWarnings("unchecked")
            List<Object> top = (List<Object>) s.resp(w0).get("candidateTopK");
            Asserts.assertEquals("a", ((Map<?, ?>) top.get(0)).get("item"), "a in window 0");
            Asserts.assertTrue(s.resp(w0).containsKey("exactTopK"), "exact topK included (trackExact)");
        } finally {
            s.stop();
        }
    }

    private static void testLateEvents() throws Exception {
        Session s = new Session();
        s.start();
        try {
            s.post("/v1/engines", engineSpec(1000L));
            s.post("/v1/engines/svc/events", Map.of("events",
                    List.of(Map.of("item", "future", "timestampMillis", 5000))));
            var late = s.post("/v1/engines/svc/events", Map.of("events",
                    List.of(Map.of("item", "late", "timestampMillis", 100))));
            Asserts.assertEquals(1L, ((Number) s.resp(late).get("droppedLate")).longValue(), "1 late");
            Asserts.assertEquals(0L, ((Number) s.resp(late).get("accepted")).longValue(), "0 accepted");
        } finally {
            s.stop();
        }
    }

    private static void testMergeCompatible() throws Exception {
        Session s = new Session();
        s.start();
        try {
            s.post("/v1/engines", engineSpec(0L));
            var sketch = s.get("/v1/engines/svc/sketch");
            // Empty but same layout -> merge succeeds (adds zero).
            var merged = s.post("/v1/engines/svc/merge", Map.of("sketch", s.resp(sketch)));
            Asserts.assertEquals(200, merged.statusCode(), "compatible merge 200");
        } finally {
            s.stop();
        }
    }

    private static void testMergeRejectSeed() throws Exception {
        Session s = new Session();
        s.start();
        try {
            s.post("/v1/engines", engineSpec(0L));
            Map<String, Object> otherSpec = engineSpec(0L);
            otherSpec.put("id", "other");
            otherSpec.put("seed", 43);
            s.post("/v1/engines", otherSpec);
            var otherSketch = s.get("/v1/engines/other/sketch");
            var merged = s.post("/v1/engines/svc/merge", Map.of("sketch", s.resp(otherSketch)));
            Asserts.assertEquals(409, merged.statusCode(), "seed mismatch -> 409");
            Asserts.assertEquals("sketch_incompatible", s.resp(merged).get("error"), "error code");
        } finally {
            s.stop();
        }
    }

    private static void testMergeRejectWidth() throws Exception {
        Session s = new Session();
        s.start();
        try {
            s.post("/v1/engines", engineSpec(0L));
            Map<String, Object> otherSpec = engineSpec(0L);
            otherSpec.put("id", "other");
            otherSpec.put("width", 128);
            s.post("/v1/engines", otherSpec);
            var otherSketch = s.get("/v1/engines/other/sketch");
            var merged = s.post("/v1/engines/svc/merge", Map.of("sketch", s.resp(otherSketch)));
            Asserts.assertEquals(409, merged.statusCode(), "width mismatch -> 409");
        } finally {
            s.stop();
        }
    }

    private static void testMergeRejectDepth() throws Exception {
        Session s = new Session();
        s.start();
        try {
            s.post("/v1/engines", engineSpec(0L));
            Map<String, Object> otherSpec = engineSpec(0L);
            otherSpec.put("id", "other");
            otherSpec.put("depth", 6);
            s.post("/v1/engines", otherSpec);
            var otherSketch = s.get("/v1/engines/other/sketch");
            var merged = s.post("/v1/engines/svc/merge", Map.of("sketch", s.resp(otherSketch)));
            Asserts.assertEquals(409, merged.statusCode(), "depth mismatch -> 409");
        } finally {
            s.stop();
        }
    }

    private static void testErrors() throws Exception {
        Session s = new Session();
        s.start();
        try {
            // Invalid JSON body.
            HttpRequest raw = HttpRequest.newBuilder(URI.create(s.base + "/v1/engines"))
                    .header("Content-Type", "application/json")
                    .POST(HttpRequest.BodyPublishers.ofString("{not json"))
                    .build();
            var bad = s.client.send(raw, HttpResponse.BodyHandlers.ofString());
            Asserts.assertEquals(400, bad.statusCode(), "invalid JSON -> 400");
            Asserts.assertEquals("invalid_json", s.resp(bad).get("error"), "error code");

            // Unknown route.
            Asserts.assertEquals(404, s.get("/v1/nope").statusCode(), "unknown route 404");

            // Unknown engine.
            Asserts.assertEquals(404, s.get("/v1/engines/ghost/topk").statusCode(), "ghost 404");
        } finally {
            s.stop();
        }
    }

    private static void testEpsilonConfig() throws Exception {
        Session s = new Session();
        s.start();
        try {
            Map<String, Object> spec = new LinkedHashMap<>();
            spec.put("id", "eps");
            spec.put("epsilon", 0.01);
            spec.put("delta", 0.01);
            spec.put("candidateCapacity", 3);
            var created = s.post("/v1/engines", spec);
            Asserts.assertEquals(201, created.statusCode(), "created from epsilon/delta");
            var stats = s.get("/v1/engines/eps");
            Asserts.assertEquals(272L, ((Number) s.resp(stats).get("width")).longValue(),
                    "width = ceil(e/0.01)");
            Asserts.assertEquals(5L, ((Number) s.resp(stats).get("depth")).longValue(),
                    "depth = ceil(ln(1/0.01))");
        } finally {
            s.stop();
        }
    }

    private static void testAdvanceRefused() throws Exception {
        ManualEnvironment env = new ManualEnvironment(0L);
        HttpEventServer server = HttpEventServer.start(0, env, env, false);
        try {
            HttpClient client = HttpClient.newHttpClient();
            HttpRequest req = HttpRequest.newBuilder(URI.create(
                            "http://localhost:" + server.port() + "/v1/admin/advance-time"))
                    .header("Content-Type", "application/json")
                    .POST(HttpRequest.BodyPublishers.ofString("{\"millis\":5}"))
                    .build();
            var resp = client.send(req, HttpResponse.BodyHandlers.ofString());
            Asserts.assertEquals(403, resp.statusCode(), "advance refused in real-time mode");
        } finally {
            server.close();
        }
    }
}
