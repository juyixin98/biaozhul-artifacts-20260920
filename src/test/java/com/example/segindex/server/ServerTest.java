package com.example.segindex.server;

import com.example.segindex.Index;
import com.example.segindex.IndexConfig;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.file.Path;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

/** End-to-end test of the JSON API over HTTP. */
class ServerTest {

    @TempDir
    Path dir;

    private Index index;
    private IndexServer server;
    private final HttpClient client = HttpClient.newHttpClient();
    private final ObjectMapper mapper = new ObjectMapper();
    private String base;

    @BeforeEach
    void start() throws Exception {
        index = Index.open(dir, IndexConfig.builder().backgroundMergeEnabled(false).build());
        server = new IndexServer(index, 0);
        server.start();
        base = "http://localhost:" + server.port();
    }

    @AfterEach
    void stop() {
        server.stop();
        index.close();
    }

    @Test
    void addSearchDeleteAndStatusOverHttp() throws Exception {
        JsonNode r1 = post("/docs", "{\"id\":\"a\",\"text\":\"the quick brown fox\"}");
        assertEquals(1, r1.get("generation").asLong());
        post("/docs", "{\"id\":\"b\",\"text\":\"quick silver\"}");
        JsonNode r3 = post("/docs", "{\"id\":\"a\",\"text\":\"quick and quiet\"}");
        assertEquals(2, r3.get("generation").asLong(), "id reuse must bump generation");

        JsonNode search = get("/search?q=quick");
        assertEquals(2, search.get("total").asInt());
        JsonNode hits = search.get("hits");
        for (JsonNode hit : hits) {
            if (hit.get("id").asText().equals("a")) {
                assertEquals(2, hit.get("generation").asInt());
                assertEquals("quick and quiet", hit.get("text").asText());
            }
        }

        JsonNode del = delete("/docs/b");
        assertTrue(del.get("deleted").asBoolean());
        JsonNode afterDelete = get("/search?q=quick");
        assertEquals(1, afterDelete.get("total").asInt());

        JsonNode segments = get("/segments");
        assertTrue(segments.get("segmentCount").asInt() >= 1);

        JsonNode merged = post("/merge", "");
        assertTrue(merged.get("segments").size() >= 1);

        assertEquals("ok", get("/health").get("status").asText());
    }

    @Test
    void badRequestsReturn4xx() throws Exception {
        assertEquals(400, raw("POST", "/docs", "{\"id\":1}").statusCode());
        assertEquals(400, raw("GET", "/search", null).statusCode());
        assertEquals(405, raw("GET", "/docs", null).statusCode());
    }

    private JsonNode post(String path, String body) throws Exception {
        return mapper.readTree(raw("POST", path, body).body());
    }

    private JsonNode get(String path) throws Exception {
        return mapper.readTree(raw("GET", path, null).body());
    }

    private JsonNode delete(String path) throws Exception {
        return mapper.readTree(raw("DELETE", path, null).body());
    }

    private HttpResponse<String> raw(String method, String path, String body) throws Exception {
        HttpRequest.Builder b = HttpRequest.newBuilder(URI.create(base + path));
        if (body != null) {
            b.method(method, HttpRequest.BodyPublishers.ofString(body));
        } else {
            b.method(method, HttpRequest.BodyPublishers.noBody());
        }
        return client.send(b.build(), HttpResponse.BodyHandlers.ofString());
    }
}
