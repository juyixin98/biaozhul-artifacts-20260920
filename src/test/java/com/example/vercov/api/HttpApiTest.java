package com.example.vercov.api;

import com.example.vercov.store.VersionStore;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

class HttpApiTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    private HttpApi api;
    private HttpClient client;
    private String base;

    @BeforeEach
    void setUp() throws Exception {
        api = new HttpApi(new VersionStore(), 0);
        api.start();
        base = "http://localhost:" + api.port();
        client = HttpClient.newHttpClient();
    }

    @AfterEach
    void tearDown() {
        api.stop();
    }

    @Test
    void metaReportsTzdbVersion() throws Exception {
        JsonNode body = get("/api/meta", 200);
        assertTrue(body.hasNonNull("tzdbVersion"));
        assertFalse(body.get("tzdbVersion").asText().isBlank());
        assertTrue(body.get("availableZoneCount").asInt() > 0);
    }

    @Test
    void fullLifecycleOverHttp() throws Exception {
        post("/api/versions", """
                {"id":"base","priority":1,"intervals":[{"start":0,"end":10,"label":"L1"}]}
                """, 200);
        post("/api/versions", """
                {"id":"patch","priority":2,"intervals":[{"start":3,"end":6,"label":"H1"}]}
                """, 200);

        JsonNode coverage = get("/api/coverage?from=0&to=10", 200);
        assertEquals(3, coverage.size());
        assertEquals("base", coverage.get(0).get("versionId").asText());
        assertEquals(0, coverage.get(0).get("start").asLong());
        assertEquals(3, coverage.get(0).get("end").asLong());
        assertEquals("patch", coverage.get(1).get("versionId").asText());
        assertEquals("base", coverage.get(2).get("versionId").asText());

        JsonNode point = get("/api/point?at=4", 200);
        assertEquals("patch", point.get("versionId").asText());

        JsonNode conflict = post("/api/versions", """
                {"id":"clash","priority":2,"intervals":[{"start":5,"end":8}]}
                """, 409);
        assertEquals("conflict", conflict.get("error").asText());

        request("DELETE", "/api/versions/patch", null, 200);
        JsonNode afterDelete = get("/api/coverage?from=0&to=10", 200);
        assertEquals(1, afterDelete.size());
        assertEquals("base", afterDelete.get(0).get("versionId").asText());

        request("DELETE", "/api/versions/patch", null, 404);
    }

    @Test
    void invalidInputGetsBadRequest() throws Exception {
        post("/api/versions", """
                {"id":"bad","priority":1,"intervals":[{"start":9,"end":2}]}
                """, 400);
        post("/api/versions", """
                {"id":"bad","priority":1}
                """, 400);
        post("/api/versions", "not json", 400);
        get("/api/coverage?from=10&to=0", 400);
        get("/api/coverage?from=abc&to=10", 400);
    }

    @Test
    void timeAxisUsesEpochSeconds() throws Exception {
        // 2026-01-01T00:00:00Z = 1767225600
        post("/api/versions", """
                {"id":"tz-rule","priority":1,"intervals":[{"start":1767225600,"end":1767312000,"label":"2026-01-01 UTC day"}]}
                """, 200);
        JsonNode point = get("/api/point?at=1767225600", 200);
        assertEquals("tz-rule", point.get("versionId").asText());
        // exclusive end: last second of the day is out
        JsonNode outside = get("/api/point?at=1767312000", 200);
        assertTrue(outside.isNull());
    }

    private JsonNode get(String path, int expectedStatus) throws Exception {
        return request("GET", path, null, expectedStatus);
    }

    private JsonNode post(String path, String json, int expectedStatus) throws Exception {
        return request("POST", path, json, expectedStatus);
    }

    private JsonNode request(String method, String path, String json, int expectedStatus)
            throws Exception {
        HttpRequest.Builder builder = HttpRequest.newBuilder(URI.create(base + path));
        if (json == null) {
            builder.method(method, HttpRequest.BodyPublishers.noBody());
        } else {
            builder.header("Content-Type", "application/json")
                    .method(method, HttpRequest.BodyPublishers.ofString(json));
        }
        HttpResponse<String> response =
                client.send(builder.build(), HttpResponse.BodyHandlers.ofString());
        assertEquals(expectedStatus, response.statusCode(), "response: " + response.body());
        return MAPPER.readTree(response.body());
    }
}
