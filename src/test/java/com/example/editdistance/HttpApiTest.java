package com.example.editdistance;

import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

class HttpApiTest {

    private static HttpApi api;
    private static HttpClient client;
    private static String base;

    @BeforeAll
    static void startServer() throws IOException {
        SearchService service = new SearchService(Normalize.NFC);
        service.rebuild(CorpusGenerator.generate(300, 42L));
        api = new HttpApi(service, 300, 42L);
        api.start(0); // ephemeral port
        base = "http://localhost:" + api.port();
        client = HttpClient.newHttpClient();
    }

    @AfterAll
    static void stopServer() {
        api.stop();
    }

    private static HttpResponse<String> post(String path, String json) throws Exception {
        HttpRequest request = HttpRequest.newBuilder(URI.create(base + path))
                .header("Content-Type", "application/json; charset=utf-8")
                .POST(HttpRequest.BodyPublishers.ofString(json, StandardCharsets.UTF_8))
                .build();
        return client.send(request, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
    }

    private static HttpResponse<String> get(String path) throws Exception {
        return client.send(HttpRequest.newBuilder(URI.create(base + path)).GET().build(),
                HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
    }

    @Test
    void healthAndCorpus() throws Exception {
        assertEquals(200, get("/health").statusCode());
        HttpResponse<String> corpus = get("/corpus");
        assertEquals(200, corpus.statusCode());
        Map<String, Object> body = Json.parseObject(corpus.body());
        assertEquals("NFC", body.get("normalize"));
        assertTrue(((Number) body.get("size")).intValue() > 0);
    }

    @Test
    @SuppressWarnings("unchecked")
    void searchFindsExactAndFuzzy() throws Exception {
        HttpResponse<String> response = post("/search", "{\"query\":\"apple\",\"k\":2,\"limit\":10}");
        assertEquals(200, response.statusCode());
        Map<String, Object> body = Json.parseObject(response.body());
        List<Object> matches = (List<Object>) body.get("matches");
        assertTrue(!matches.isEmpty(), "expected at least one match for apple");
        Map<String, Object> first = (Map<String, Object>) matches.get(0);
        assertEquals("apple", first.get("term"));
        assertEquals(0L, ((Number) first.get("distance")).longValue());
        // Funnel stats present and consistent.
        Map<String, Object> stats = (Map<String, Object>) body.get("stats");
        int corpusSize = ((Number) stats.get("corpusSize")).intValue();
        int lengthPassed = ((Number) stats.get("lengthPassed")).intValue();
        int candidates = ((Number) stats.get("candidates")).intValue();
        assertTrue(corpusSize >= lengthPassed && lengthPassed >= candidates);
    }

    @Test
    @SuppressWarnings("unchecked")
    void searchHandlesUnicodeAndEmptyQuery() throws Exception {
        // NFD query for an NFC corpus term: normalization must make them match.
        HttpResponse<String> response = post("/search",
                "{\"query\":\"cafe\u0301\",\"k\":0}");
        assertEquals(200, response.statusCode());
        Map<String, Object> body = Json.parseObject(response.body());
        List<Object> matches = (List<Object>) body.get("matches");
        assertTrue(matches.stream()
                .map(m -> (Map<String, Object>) m)
                .anyMatch(m -> m.get("term").equals("caf\u00E9") && ((Number) m.get("distance")).intValue() == 0),
                "NFD query must match NFC corpus term at distance 0");

        // Empty query with k=0 matches only the empty term (corpus always contains it).
        HttpResponse<String> empty = post("/search", "{\"query\":\"\",\"k\":0}");
        assertEquals(200, empty.statusCode());
        Map<String, Object> emptyBody = Json.parseObject(empty.body());
        List<Object> emptyMatches = (List<Object>) emptyBody.get("matches");
        assertEquals(1, emptyMatches.size());
        assertEquals("", ((Map<String, Object>) emptyMatches.get(0)).get("term"));
    }

    @Test
    void rejectsBadRequests() throws Exception {
        assertEquals(400, post("/search", "{\"k\":2}").statusCode()); // missing query
        assertEquals(400, post("/search", "{\"query\":\"a\",\"k\":-1}").statusCode());
        assertEquals(400, post("/search", "{\"query\":\"a\",\"k\":99}").statusCode());
        assertEquals(400, post("/search", "not json").statusCode());
        assertEquals(405, get("/search").statusCode()); // GET not allowed on /search
    }

    @Test
    void reloadCorpus() throws Exception {
        HttpResponse<String> response = post("/corpus/reload", "{\"size\":120,\"seed\":1}");
        assertEquals(200, response.statusCode());
        Map<String, Object> body = Json.parseObject(response.body());
        assertTrue(((Number) body.get("size")).intValue() > 0);
        // Restore the default corpus for other tests.
        post("/corpus/reload", "{\"size\":300,\"seed\":42}");
    }
}
