package com.example.hlc;

import com.example.hlc.clock.VirtualClock;
import com.example.hlc.core.HybridLogicalClock;
import com.example.hlc.persist.FileHlcStateStore;
import com.example.hlc.persist.HlcStateStore;
import com.example.hlc.server.HlcServer;
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
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

class HlcServerTest {

    private final ObjectMapper mapper = new ObjectMapper();
    private final HttpClient client = HttpClient.newHttpClient();

    private HlcServer server;
    private String base;

    @TempDir
    Path tempDir;

    @BeforeEach
    void startServer() {
        VirtualClock vc = new VirtualClock(1_727_000_000_000L);
        HybridLogicalClock hlc = HybridLogicalClock.builder(vc, "test-node").build();
        HlcStateStore store = new FileHlcStateStore(tempDir.resolve("state.json"), mapper);
        server = new HlcServer(hlc, store, mapper, 0); // ephemeral port
        server.start();
        base = "http://localhost:" + server.port();
    }

    @AfterEach
    void stopServer() {
        server.stop();
    }

    private JsonNode post(String path, String body) throws Exception {
        HttpRequest request = HttpRequest.newBuilder(URI.create(base + path))
                .POST(HttpRequest.BodyPublishers.ofString(body))
                .header("Content-Type", "application/json")
                .build();
        HttpResponse<String> response = client.send(request, HttpResponse.BodyHandlers.ofString());
        return mapper.readTree(response.body());
    }

    private JsonNode get(String path) throws Exception {
        HttpResponse<String> response = client.send(
                HttpRequest.newBuilder(URI.create(base + path)).GET().build(),
                HttpResponse.BodyHandlers.ofString());
        return mapper.readTree(response.body());
    }

    @Test
    void sendReturnsFreshMonotonicTimestamps() throws Exception {
        JsonNode first = post("/api/send", "{}");
        JsonNode second = post("/api/send", "{}");

        assertTrue(first.get("ok").asBoolean());
        assertTrue(second.get("ok").asBoolean());
        JsonNode t1 = first.get("data");
        JsonNode t2 = second.get("data");
        assertEquals("test-node", t1.get("nodeId").asText());
        assertTrue(t2.get("logical").asInt() > t1.get("logical").asInt()
                || t2.get("physicalMillis").asLong() > t1.get("physicalMillis").asLong());
    }

    @Test
    void receiveMergesRemoteTimestamp() throws Exception {
        String body = "{\"timestamp\":{\"physicalMillis\":1727000005000,\"logical\":3,\"nodeId\":\"remote\"}}";
        JsonNode response = post("/api/receive", body);

        assertTrue(response.get("ok").asBoolean(), "unexpected error: " + response);
        JsonNode data = response.get("data");
        assertEquals(1_727_000_005_000L, data.get("physicalMillis").asLong());
        assertEquals(4, data.get("logical").asInt());
        assertEquals("test-node", data.get("nodeId").asText());
    }

    @Test
    void receiveRejectsMalformedRequests() throws Exception {
        JsonNode notJson = post("/api/receive", "this is not json");
        assertFalse(notJson.get("ok").asBoolean());
        assertTrue(notJson.get("error").asText().contains("not valid JSON"));

        JsonNode missingField = post("/api/receive", "{}");
        assertFalse(missingField.get("ok").asBoolean());
        assertTrue(missingField.get("error").asText().contains("timestamp"));
    }

    @Test
    void metaReportsTzdbVersion() throws Exception {
        JsonNode meta = get("/api/meta");
        assertTrue(meta.get("ok").asBoolean());
        JsonNode data = meta.get("data");
        assertEquals("test-node", data.get("nodeId").asText());
        String tzdb = data.get("tzdbVersion").asText();
        assertFalse(tzdb.isBlank(), "tzdb version must be reported");
        System.out.println("tzdbVersion=" + tzdb + " systemZone=" + data.get("systemZone").asText());
    }

    @Test
    void nowDoesNotAdvanceClock() throws Exception {
        JsonNode n1 = get("/api/now").get("data");
        JsonNode n2 = get("/api/now").get("data");
        assertEquals(n1, n2);
    }
}
