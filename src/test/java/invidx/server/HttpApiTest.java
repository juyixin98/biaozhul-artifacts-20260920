package invidx.server;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import invidx.engine.IndexConfig;
import invidx.engine.InvertedIndex;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.file.Path;
import java.time.Duration;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

class HttpApiTest {

    private final ObjectMapper mapper = new ObjectMapper();
    private final HttpClient client = HttpClient.newHttpClient();

    @Test
    void fullDocumentLifecycleOverHttp(@TempDir Path tmp) throws Exception {
        IndexConfig cfg = new IndexConfig(2, false, Duration.ofMinutes(10), 3);
        try (InvertedIndex index = InvertedIndex.open(tmp, cfg, (p) -> {
        });
             JsonHttpServer server = new JsonHttpServer(index, 0)) {
            server.start();
            int port = server.port();
            URI base = URI.create("http://127.0.0.1:" + port);

            // Auto id.
            JsonNode created = post(base + "/documents", """
                    {"text":"alpha bravo"}""");
            int id = created.get("id").asInt();
            assertEquals(1, created.get("gen").asInt());

            // Explicit id update.
            JsonNode updated = post(base + "/documents",
                    "{\"id\":" + id + ",\"text\":\"alpha charlie\"}");
            assertEquals(2, updated.get("gen").asInt());

            JsonNode search = get(base + "/search?q=alpha");
            assertEquals(1, search.get("total").asInt());
            assertEquals("alpha charlie",
                    search.get("hits").get(0).get("text").asText());
            assertEquals(0, get(base + "/search?q=bravo").get("total").asInt());

            // Boolean queries via POST body.
            JsonNode andResult = post(base + "/search",
                    "{\"q\":\"AND:alpha,charlie\"}");
            assertEquals(1, andResult.get("total").asInt());
            JsonNode orResult = post(base + "/search",
                    "{\"q\":\"OR:charlie,missing\"}");
            assertEquals(1, orResult.get("total").asInt());

            // Delete then 404.
            HttpResponse<String> deleted = request("DELETE",
                    base + "/documents/" + id, null);
            assertEquals(200, deleted.statusCode());
            HttpResponse<String> gone = request("GET",
                    base + "/documents/" + id, null);
            assertEquals(404, gone.statusCode());
            assertEquals(0, get(base + "/search?q=alpha").get("total").asInt());

            // Second delete reports the id as not found.
            HttpResponse<String> deleteAgain = request("DELETE",
                    base + "/documents/" + id, null);
            assertEquals(404, deleteAgain.statusCode());

            // Validation errors.
            HttpResponse<String> bad = request("POST",
                    base + "/documents", "{\"text\":\"\"}");
            assertEquals(400, bad.statusCode());
            HttpResponse<String> badQuery = request("GET",
                    base + "/search", null);
            assertEquals(400, badQuery.statusCode());

            // Stats and admin endpoints.
            JsonNode stats = get(base + "/stats");
            assertTrue(stats.get("liveDocs").asInt() >= 0);
            JsonNode flushed = post(base + "/flush", "{}");
            assertTrue(flushed.get("flushed").asBoolean());
            JsonNode merged = post(base + "/merge", "{}");
            assertTrue(merged.has("mergedSegments"));
        }
    }

    private JsonNode get(String url) throws Exception {
        HttpResponse<String> response = request("GET", url, null);
        assertEquals(200, response.statusCode(), response.body());
        return mapper.readTree(response.body());
    }

    private JsonNode post(String url, String body) throws Exception {
        HttpResponse<String> response = request("POST", url, body);
        assertEquals(200, response.statusCode(), response.body());
        return mapper.readTree(response.body());
    }

    private JsonNode delete(String url) throws Exception {
        HttpResponse<String> response = request("DELETE", url, null);
        assertEquals(200, response.statusCode(), response.body());
        return mapper.readTree(response.body());
    }

    private HttpResponse<String> request(String method, String url, String body)
            throws Exception {
        HttpRequest.Builder b = HttpRequest.newBuilder(URI.create(url));
        if (body != null) {
            b.header("Content-Type", "application/json")
                    .method(method, HttpRequest.BodyPublishers.ofString(body));
        } else {
            b.method(method, HttpRequest.BodyPublishers.noBody());
        }
        return client.send(b.build(), HttpResponse.BodyHandlers.ofString());
    }
}
