package com.example.vic;

import com.example.vic.http.ApiServer;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

class ApiServerTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    private ApiServer server;
    private HttpClient client;
    private String base;

    @BeforeEach
    void startServer() throws IOException {
        server = ApiServer.create(0);
        server.start();
        base = "http://localhost:" + server.port();
        client = HttpClient.newHttpClient();
    }

    @AfterEach
    void stopServer() {
        server.stop();
    }

    private record Response(int status, JsonNode body) {
    }

    private Response call(String method, String path, String json) throws Exception {
        HttpRequest.Builder builder = HttpRequest.newBuilder(URI.create(base + path));
        if (json == null) {
            builder.method(method, HttpRequest.BodyPublishers.noBody());
        } else {
            builder.method(method, HttpRequest.BodyPublishers.ofString(json))
                    .header("Content-Type", "application/json");
        }
        HttpResponse<String> response = client.send(builder.build(), HttpResponse.BodyHandlers.ofString());
        return new Response(response.statusCode(), MAPPER.readTree(response.body()));
    }

    private void putVersion(String id, int priority) throws Exception {
        Response r = call("PUT", "/api/versions", "{\"id\":\"" + id + "\",\"priority\":" + priority + "}");
        assertEquals(200, r.status());
        assertTrue(r.body().get("success").asBoolean());
    }

    private void putRule(String id, String versionId, long start, long end) throws Exception {
        Response r = call("PUT", "/api/rules",
                "{\"id\":\"" + id + "\",\"versionId\":\"" + versionId + "\",\"start\":" + start + ",\"end\":" + end + "}");
        assertEquals(200, r.status(), () -> "putRule failed: " + r.body());
    }

    @Test
    void metaReportsTzdbVersion() throws Exception {
        Response r = call("GET", "/api/meta", null);
        assertEquals(200, r.status());
        assertTrue(r.body().get("data").hasNonNull("tzdbVersion"));
        assertFalse(r.body().get("data").get("tzdbVersion").asText().isBlank());
    }

    @Test
    void fullOverlayFlowOverHttp() throws Exception {
        putVersion("v1", 1);
        putVersion("v2", 2);
        putRule("r1", "v1", 0, 10);
        putRule("r2", "v2", 2, 8);

        Response computed = call("POST", "/api/compute", "{}");
        assertEquals(200, computed.status());
        JsonNode segments = computed.body().get("data").get("segments");
        assertEquals(3, segments.size());
        assertEquals("r1", segments.get(0).get("ruleId").asText());
        assertEquals("v2", segments.get(1).get("versionId").asText());
        assertEquals(2, segments.get(1).get("start").asLong());
        assertEquals(8, segments.get(1).get("end").asLong());

        Response query = call("POST", "/api/query", "{\"point\":5}");
        assertEquals("r2", query.body().get("data").get("effective").get("ruleId").asText());
        Response gap = call("POST", "/api/query", "{\"point\":42}");
        assertTrue(gap.body().get("data").get("effective").isNull());
    }

    @Test
    void equalPriorityConflictReturns409() throws Exception {
        putVersion("v1", 1);
        putRule("r1", "v1", 0, 6);
        Response conflict = call("PUT", "/api/rules",
                "{\"id\":\"r2\",\"versionId\":\"v1\",\"start\":4,\"end\":9}");
        assertEquals(409, conflict.status());
        assertFalse(conflict.body().get("success").asBoolean());
        assertTrue(conflict.body().get("error").asText().contains("equal-priority conflict"));
    }

    @Test
    void deleteVersionTriggersRecompute() throws Exception {
        putVersion("v1", 1);
        putVersion("v2", 2);
        putRule("r1", "v1", 0, 10);
        putRule("r2", "v2", 2, 8);

        Response deleted = call("DELETE", "/api/versions/v2", null);
        assertEquals(200, deleted.status());

        Response computed = call("POST", "/api/compute", "{}");
        JsonNode segments = computed.body().get("data").get("segments");
        assertEquals(1, segments.size());
        assertEquals("r1", segments.get(0).get("ruleId").asText());
        assertEquals(0, segments.get(0).get("start").asLong());
        assertEquals(10, segments.get(0).get("end").asLong());
    }

    @Test
    void unknownVersionAndRuleReturn404() throws Exception {
        Response r = call("DELETE", "/api/versions/nope", null);
        assertEquals(404, r.status());
        putVersion("v1", 1);
        Response r2 = call("PUT", "/api/rules",
                "{\"id\":\"r1\",\"versionId\":\"ghost\",\"start\":0,\"end\":5}");
        assertEquals(404, r2.status());
    }

    @Test
    void malformedBodyReturns400() throws Exception {
        Response r = call("PUT", "/api/versions", "{not json");
        assertEquals(400, r.status());
        Response missing = call("PUT", "/api/rules", "{\"id\":\"r1\"}");
        assertEquals(400, missing.status());
    }
}
