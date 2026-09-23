package com.example.uninorm;

import java.net.URI;
import java.net.URLEncoder;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.Map;

import static com.example.uninorm.TestSupport.assertEquals;
import static com.example.uninorm.TestSupport.assertTrue;

/**
 * Black-box tests against the real {@link SearchServer}: spin it up on an
 * ephemeral port and talk JSON over loopback using only the JDK HTTP client.
 */
public final class HttpServiceTest {

    private HttpServiceTest() {
    }

    private static String cps(int... codePoints) {
        return new String(codePoints, 0, codePoints.length);
    }

    public static void endToEndHttp() throws Exception {
        SearchEngine engine = new SearchEngine();
        for (Document d : Corpus.documents()) {
            engine.addDocument(d);
        }
        SearchServer server = new SearchServer(engine);
        server.start(0);
        int port = server.port();
        try {
            HttpClient client = HttpClient.newHttpClient();
            String base = "http://127.0.0.1:" + port;

            // GET /health
            HttpResponse<String> health = get(client, base + "/health");
            assertEquals(200, health.statusCode(), "health status");
            assertTrue(health.body().contains("\"status\":\"ok\""),
                    "health body");

            // POST /search, body carries a non-ASCII query.
            String postBody = Json.write(Map.of("query", "strasse"));
            HttpResponse<String> post = post(client, base + "/search", postBody);
            assertEquals(200, post.statusCode(), "POST search status");
            assertTrue(post.body().contains("\"count\":4"),
                    "four Straße hits via POST, got: " + post.body());
            assertTrue(post.body().contains(cps('S', 't', 'r', 'a', 0x00DF, 'e')),
                    "original ß spelling echoed back");

            // POST with limit
            String limited = Json.write(Map.of("query",
                    cps('c', 'a', 'f', 'e', 0x0301), "limit", 2));
            HttpResponse<String> limitedResp =
                    post(client, base + "/search", limited);
            assertTrue(limitedResp.body().contains("\"count\":2"),
                    "limit respected: " + limitedResp.body());

            // GET /search with URL-encoded ligature query "ﬁle".
            String q = URLEncoder.encode(cps(0xFB01, 'l', 'e'),
                    StandardCharsets.UTF_8);
            HttpResponse<String> getSearch =
                    get(client, base + "/search?q=" + q);
            assertEquals(200, getSearch.statusCode(), "GET search status");
            assertTrue(getSearch.statusCode() == 200, "200 ok");
            // normalized query must be plain "file"
            assertTrue(getSearch.body().contains(
                    "\"normalizedQuery\":\"file\""),
                    "ligature query normalizes to file: " + getSearch.body());

            // GET /normalize
            String normQ = URLEncoder.encode("Straße", StandardCharsets.UTF_8);
            HttpResponse<String> norm =
                    get(client, base + "/normalize?text=" + normQ);
            assertEquals(200, norm.statusCode(), "normalize status");
            assertTrue(norm.body().contains("\"normalized\":\"strasse\""),
                "normalize endpoint: " + norm.body());

            // Error handling.
            HttpResponse<String> badPost =
                    post(client, base + "/search", "not json");
            assertEquals(400, badPost.statusCode(), "malformed JSON -> 400");
            HttpResponse<String> noQuery =
                    get(client, base + "/search");
            assertEquals(400, noQuery.statusCode(), "missing q -> 400");
            HttpResponse<String> wrongMethod = client.send(
                    HttpRequest.newBuilder(URI.create(base + "/health"))
                            .DELETE().build(),
                    HttpResponse.BodyHandlers.ofString());
            assertEquals(405, wrongMethod.statusCode(), "DELETE -> 405");

            // GET /docs lists every corpus document with a normalized key.
            HttpResponse<String> docs = get(client, base + "/docs");
            assertEquals(200, docs.statusCode(), "docs status");
            assertTrue(docs.body().contains("doc-emoji-math"),
                    "docs contains doc ids");
        } finally {
            server.stop();
        }
    }

    private static HttpResponse<String> get(HttpClient client, String url)
            throws Exception {
        return client.send(
                HttpRequest.newBuilder(URI.create(url)).GET().build(),
                HttpResponse.BodyHandlers.ofString());
    }

    private static HttpResponse<String> post(HttpClient client, String url,
                                             String body) throws Exception {
        return client.send(HttpRequest.newBuilder(URI.create(url))
                        .header("Content-Type", "application/json; charset=utf-8")
                        .POST(HttpRequest.BodyPublishers.ofString(body,
                                StandardCharsets.UTF_8)).build(),
                HttpResponse.BodyHandlers.ofString());
    }
}
