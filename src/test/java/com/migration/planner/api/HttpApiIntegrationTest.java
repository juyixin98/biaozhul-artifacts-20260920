package com.migration.planner.api;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.migration.planner.graph.GraphLoader;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

class HttpApiIntegrationTest {

    private static HttpApiServer server;
    private static HttpClient client;
    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static String base;

    @BeforeAll
    static void startServer() {
        PlanService service = new PlanService(new GraphLoader(MAPPER).loadDefault());
        server = HttpApiServer.start(service, MAPPER, 0);
        base = "http://127.0.0.1:" + server.port();
        client = HttpClient.newHttpClient();
    }

    @AfterAll
    static void stopServer() {
        server.close();
    }

    @Test
    void postPlanReturnsStepsAndCheckpoints() throws Exception {
        JsonNode body = post("/plan", """
                {"from":"v1","to":"v6","context":{"maintenance_window":true,"backup_verified":true}}
                """, 200);
        assertTrue(body.get("ok").asBoolean());
        assertEquals(11, body.get("totalCost").asLong());
        assertEquals(5, body.get("steps").size());
        assertEquals(5, body.get("checkpoints").size());
        assertEquals(2, body.get("alternatives").size());
        assertTrue(body.get("tzdbVersion").asText().matches("\\d{4}[a-z]"));
    }

    @Test
    void noPathReturns422() throws Exception {
        JsonNode body = post("/plan", """
                {"from":"v6","to":"v1","context":{}}
                """, 422);
        assertFalse(body.get("ok").asBoolean());
        assertEquals("NO_PATH", body.get("error").get("code").asText());
        assertTrue(body.get("error").get("details").has("reachableVersions"));
    }

    @Test
    void unknownVersionReturns422() throws Exception {
        JsonNode body = post("/plan", """
                {"from":"v9","to":"v1","context":{}}
                """, 422);
        assertEquals("UNKNOWN_VERSION", body.get("error").get("code").asText());
    }

    @Test
    void malformedJsonReturns400() throws Exception {
        JsonNode body = post("/plan", "{not json", 400);
        assertEquals("INVALID_REQUEST", body.get("error").get("code").asText());
    }

    @Test
    void missingFieldsReturn400() throws Exception {
        JsonNode body = post("/plan", """
                {"to":"v1"}
                """, 400);
        assertEquals("INVALID_REQUEST", body.get("error").get("code").asText());
    }

    @Test
    void healthReportsTzdbVersion() throws Exception {
        HttpRequest request = HttpRequest.newBuilder(URI.create(base + "/health")).GET().build();
        HttpResponse<String> response = client.send(request, HttpResponse.BodyHandlers.ofString());
        assertEquals(200, response.statusCode());
        JsonNode body = MAPPER.readTree(response.body());
        assertEquals("ok", body.get("status").asText());
        assertTrue(body.get("tzdbVersion").asText().matches("\\d{4}[a-z]"));
        assertEquals(10, body.get("nodeCount").asInt());
    }

    @Test
    void graphEndpointExposesFixture() throws Exception {
        HttpRequest request = HttpRequest.newBuilder(URI.create(base + "/graph")).GET().build();
        HttpResponse<String> response = client.send(request, HttpResponse.BodyHandlers.ofString());
        assertEquals(200, response.statusCode());
        JsonNode body = MAPPER.readTree(response.body());
        assertEquals("schema-migration-fixture", body.get("graphId").asText());
        assertEquals(13, body.get("edges").size());
    }

    private JsonNode post(String path, String json, int expectedStatus) throws IOException, InterruptedException {
        HttpRequest request = HttpRequest.newBuilder(URI.create(base + path))
                .POST(HttpRequest.BodyPublishers.ofString(json))
                .header("Content-Type", "application/json")
                .build();
        HttpResponse<String> response = client.send(request, HttpResponse.BodyHandlers.ofString());
        assertEquals(expectedStatus, response.statusCode(), "body: " + response.body());
        return MAPPER.readTree(response.body());
    }
}
