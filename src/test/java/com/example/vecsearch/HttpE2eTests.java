package com.example.vecsearch;

import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.List;
import java.util.Map;

/**
 * End-to-end tests over real HTTP: boots the JDK HttpServer on an
 * ephemeral port and talks to it with the JDK HttpClient.
 */
public class HttpE2eTests {

    private static HttpClient client;
    private static String base;

    public static void run(TestFramework t) throws Exception {
        VectorStore store = new VectorStore();
        SearchService service = new SearchService(store, 8, 20, 42);
        HttpServerMain app = new HttpServerMain(service, store);
        app.start(0, 4); // ephemeral port
        base = "http://127.0.0.1:" + app.boundPort();
        client = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(5)).build();

        testHealthAndStats(t);
        testInsertAndSearch(t);
        testJsonErrors(t);
        testDeleteAndFilter(t);
        testBatchAndApprox(t);
    }

    private static void testHealthAndStats(TestFramework t) throws Exception {
        t.section("HTTP: health and stats");
        HttpResponse<String> health = get("/health");
        t.check(health.statusCode() == 200, "GET /health -> 200");
        t.check(health.body().contains("\"ok\""), "health body reports ok");

        HttpResponse<String> stats = get("/stats");
        t.check(stats.statusCode() == 200, "GET /stats -> 200");
        t.check(Json.parseObject(stats.body()).get("vectors").equals(0.0),
                "fresh server reports 0 vectors");
    }

    private static void testInsertAndSearch(TestFramework t) throws Exception {
        t.section("HTTP: insert and exact search");
        int s1 = post("/insert",
                Map.of("id", "a", "vector", List.of(0.0, 0.0), "metric", "L2")).statusCode();
        t.check(s1 == 200, "insert first vector -> 200");

        int s2 = post("/insert",
                Map.of("id", "b", "vector", List.of(1.0, 1.0),
                        "metadata", Map.of("cat", "red"), "metric", "L2")).statusCode();
        t.check(s2 == 200, "insert second vector with metadata -> 200");

        HttpResponse<String> badDim = post("/insert",
                Map.of("id", "c", "vector", List.of(1.0, 2.0, 3.0), "metric", "L2"));
        t.check(badDim.statusCode() == 400,
                "dimension mismatch over HTTP -> 400, got " + badDim.statusCode());
        t.check(badDim.body().contains("dimension mismatch"), "error body explains the mismatch");

        HttpResponse<String> zero = post("/insert",
                Map.of("id", "z", "vector", List.of(0.0, 0.0), "metric", "COSINE"));
        t.check(zero.statusCode() == 400,
                "zero vector under COSINE over HTTP -> 400, got " + zero.statusCode());

        HttpResponse<String> search = post("/search",
                Map.of("vector", List.of(0.1, 0.1), "metric", "L2", "k", 1));
        t.check(search.statusCode() == 200, "exact search -> 200");
        Map<String, Object> body = Json.parseObject(search.body());
        List<?> hits = (List<?>) body.get("hits");
        t.check(hits.size() == 1, "k=1 returns exactly one hit");
        Map<?, ?> top = (Map<?, ?>) hits.get(0);
        t.check("a".equals(top.get("id")), "nearest neighbour over HTTP is \"a\"");
        t.check(body.get("vectorDistanceCalculations").equals(2.0),
                "search response reports distance calculation count (2)");
    }

    private static void testJsonErrors(TestFramework t) throws Exception {
        t.section("HTTP: malformed requests");
        HttpResponse<String> malformed = postRaw("/insert", "{not json");
        t.check(malformed.statusCode() == 400, "malformed JSON -> 400");

        HttpResponse<String> missingMetric = post("/search",
                Map.of("vector", List.of(1.0, 1.0), "k", 1));
        t.check(missingMetric.statusCode() == 400, "search without metric -> 400");

        HttpResponse<String> unknownRoute = client.send(
                HttpRequest.newBuilder(URI.create(base + "/nope")).GET().build(),
                HttpResponse.BodyHandlers.ofString());
        t.check(unknownRoute.statusCode() == 404, "unknown route -> 404");

        HttpResponse<String> getOnPost = get("/insert");
        t.check(getOnPost.statusCode() == 405, "GET on a POST-only route -> 405");
    }

    private static void testDeleteAndFilter(TestFramework t) throws Exception {
        t.section("HTTP: deleted ids never come back, with and without filter");
        post("/insert", Map.of("id", "d1", "vector", List.of(2.0, 2.0),
                "metadata", Map.of("cat", "red", "n", 1.0), "metric", "L2"));
        post("/insert", Map.of("id", "d2", "vector", List.of(2.1, 2.1),
                "metadata", Map.of("cat", "blue"), "metric", "L2"));
        post("/insert", Map.of("id", "d3", "vector", List.of(2.2, 2.2),
                "metadata", Map.of("cat", "red", "n", 2.0), "metric", "L2"));

        HttpResponse<String> del = post("/delete", Map.of("id", "d1"));
        t.check(del.statusCode() == 200 && Json.parseObject(del.body()).get("deleted").equals(true),
                "delete d1 -> {deleted: true}");

        // Exact, no filter.
        Map<String, Object> exact = Json.parseObject(post("/search",
                Map.of("vector", List.of(2.0, 2.0), "metric", "L2", "k", 10,
                        "mode", "exact")).body());
        t.check(noId((List<?>) exact.get("hits"), "d1"),
                "exact search after delete does not return d1");

        // Approx, no filter (forces index rebuild because data changed).
        Map<String, Object> approx = Json.parseObject(post("/search",
                Map.of("vector", List.of(2.0, 2.0), "metric", "L2", "k", 10,
                        "mode", "approx", "nprobe", 8)).body());
        t.check(noId((List<?>) approx.get("hits"), "d1"),
                "approx search after delete does not return d1 (rebuilt index)");

        // Filtered exact and approx.
        Map<String, Object> filteredExact = Json.parseObject(post("/search",
                Map.of("vector", List.of(2.0, 2.0), "metric", "L2", "k", 10,
                        "mode", "exact", "filter", Map.of("cat", "red"))).body());
        List<?> feHits = (List<?>) filteredExact.get("hits");
        t.check(noId(feHits, "d1") && onlyCat(feHits, "red"),
                "filtered exact search returns only live red vectors (d3), never deleted d1");

        Map<String, Object> filteredApprox = Json.parseObject(post("/search",
                Map.of("vector", List.of(2.0, 2.0), "metric", "L2", "k", 10,
                        "mode", "approx", "nprobe", 8, "filter", Map.of("cat", "red"))).body());
        List<?> faHits = (List<?>) filteredApprox.get("hits");
        t.check(noId(faHits, "d1") && onlyCat(faHits, "red"),
                "filtered approx search returns only live red vectors, never deleted d1");
    }

    private static void testBatchAndApprox(TestFramework t) throws Exception {
        t.section("HTTP: batch insert + cosine approx counters");
        post("/reset", Map.of());
        int status = post("/batch", Map.of(
                "metric", "COSINE",
                "vectors", List.of(
                        Map.of("id", "x1", "vector", List.of(1.0, 0.0)),
                        Map.of("id", "x2", "vector", List.of(0.9, 0.1),
                                "metadata", Map.of("g", 1.0)),
                        Map.of("id", "x3", "vector", List.of(0.0, 1.0))))).statusCode();
        t.check(status == 200, "batch insert -> 200");

        HttpResponse<String> res = post("/search",
                Map.of("vector", List.of(1.0, 0.0), "metric", "COSINE", "k", 2,
                        "mode", "approx", "nprobe", 8));
        Map<String, Object> body = Json.parseObject(res.body());
        List<?> hits = (List<?>) body.get("hits");
        t.check(hits.size() == 2 && "x1".equals(((Map<?, ?>) hits.get(0)).get("id")),
                "cosine approx finds x1 as nearest");
        t.check(((Number) body.get("centroidDistanceCalculations")).doubleValue() >= 3,
                "approx response includes centroid distance count");
        t.check(body.get("indexBuiltThisRequest").equals(true),
                "first approx request reports a freshly built index");

        // Second approx request must reuse the index (no rebuild).
        Map<String, Object> again = Json.parseObject(post("/search",
                Map.of("vector", List.of(1.0, 0.0), "metric", "COSINE", "k", 2,
                        "mode", "approx", "nprobe", 8)).body());
        t.check(again.get("indexBuiltThisRequest").equals(false),
                "follow-up approx request reuses the trained index");
    }

    // ------------------------------------------------------------------

    private static boolean noId(List<?> hits, String id) {
        return hits.stream().noneMatch(h -> id.equals(((Map<?, ?>) h).get("id")));
    }

    private static boolean onlyCat(List<?> hits, String cat) {
        return hits.stream().allMatch(h ->
                cat.equals(((Map<?, ?>) ((Map<?, ?>) h).get("metadata")).get("cat")));
    }

    private static HttpResponse<String> get(String path) throws IOException, InterruptedException {
        return client.send(
                HttpRequest.newBuilder(URI.create(base + path)).GET().build(),
                HttpResponse.BodyHandlers.ofString());
    }

    private static HttpResponse<String> post(String path, Object body)
            throws IOException, InterruptedException {
        return postRaw(path, Json.write(body));
    }

    private static HttpResponse<String> postRaw(String path, String json)
            throws IOException, InterruptedException {
        return client.send(
                HttpRequest.newBuilder(URI.create(base + path))
                        .header("Content-Type", "application/json")
                        .POST(HttpRequest.BodyPublishers.ofString(json))
                        .build(),
                HttpResponse.BodyHandlers.ofString());
    }
}
