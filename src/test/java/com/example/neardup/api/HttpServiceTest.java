package com.example.neardup.api;

import com.example.neardup.NearDupConfig;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

class HttpServiceTest {

    private static HttpService service;
    private static String base;
    private static final HttpClient CLIENT = HttpClient.newHttpClient();
    private static final ObjectMapper MAPPER = new ObjectMapper();

    @BeforeAll
    static void startServer() throws IOException {
        service = new HttpService(0); // ephemeral port
        service.start();
        base = "http://localhost:" + service.port();
    }

    @AfterAll
    static void stopServer() {
        service.stop();
    }

    private static JsonNode post(String path, String body) throws Exception {
        HttpResponse<String> response = CLIENT.send(HttpRequest.newBuilder()
                        .uri(URI.create(base + path))
                        .POST(HttpRequest.BodyPublishers.ofString(body))
                        .header("Content-Type", "application/json")
                        .build(),
                HttpResponse.BodyHandlers.ofString());
        return MAPPER.createObjectNode()
                .put("status", response.statusCode())
                .set("body", MAPPER.readTree(response.body()));
    }

    private static HttpResponse<String> get(String path) throws Exception {
        return CLIENT.send(HttpRequest.newBuilder().uri(URI.create(base + path)).GET().build(),
                HttpResponse.BodyHandlers.ofString());
    }

    @Test
    void healthEndpoint() throws Exception {
        HttpResponse<String> response = get("/health");
        assertEquals(200, response.statusCode());
        assertEquals("ok", MAPPER.readTree(response.body()).get("status").asText());
    }

    @Test
    void clustersTwoSimilarDocuments() throws Exception {
        String body = """
                {"documents":[
                  {"id":"d1","text":"the quick brown fox jumps over the lazy dog near the river bank"},
                  {"id":"d2","text":"the quick brown fox jumps over the lazy dog near the river side"},
                  {"id":"d3","text":"completely unrelated content about compilers parsers and bytecode"}
                ]}""";
        JsonNode response = post("/cluster", body);
        assertEquals(200, response.get("status").asInt());
        JsonNode clusters = response.get("body").get("clusters");
        assertEquals(1, clusters.size());
        assertEquals(2, clusters.get(0).get("memberIds").size());
        assertTrue(response.get("body").get("stats").get("verifiedPairs").asInt() >= 1);
    }

    @Test
    void sampleEndpointClustersBuiltInCorpus() throws Exception {
        JsonNode response = post("/cluster/sample", "{}");
        assertEquals(200, response.get("status").asInt());
        JsonNode stats = response.get("body").get("stats");
        assertTrue(stats.get("documentCount").asInt() > 20);
        assertTrue(stats.get("candidatePairs").asInt() >= stats.get("verifiedPairs").asInt());
        assertEquals(2, stats.get("emptyShingleDocuments").asInt());
    }

    @Test
    void corpusSampleEndpointReturnsDocuments() throws Exception {
        HttpResponse<String> response = get("/corpus/sample");
        assertEquals(200, response.statusCode());
        assertTrue(MAPPER.readTree(response.body()).get("documents").size() > 20);
    }

    @Test
    void rejectsEmptyDocumentList() throws Exception {
        JsonNode response = post("/cluster", "{\"documents\":[]}");
        assertEquals(400, response.get("status").asInt());
        assertTrue(response.get("body").has("error"));
    }

    @Test
    void rejectsDuplicateIds() throws Exception {
        String body = """
                {"documents":[
                  {"id":"x","text":"some text here"},
                  {"id":"x","text":"other text here"}
                ]}""";
        JsonNode response = post("/cluster", body);
        assertEquals(400, response.get("status").asInt());
    }

    @Test
    void rejectsBadThreshold() throws Exception {
        String body = """
                {"threshold": 1.5, "documents":[{"id":"a","text":"hello there world"}]}""";
        JsonNode response = post("/cluster", body);
        assertEquals(400, response.get("status").asInt());
    }

    @Test
    void rejectsMalformedJson() throws Exception {
        JsonNode response = post("/cluster", "{not json");
        assertEquals(400, response.get("status").asInt());
    }

    @Test
    void rejectsWrongMethod() throws Exception {
        HttpResponse<String> response = get("/cluster");
        assertEquals(405, response.statusCode());
    }

    @Test
    void defaultThresholdMatchesConfigConstant() throws Exception {
        JsonNode response = post("/cluster/sample", "{}");
        assertEquals(NearDupConfig.DEFAULT_THRESHOLD,
                response.get("body").get("config").get("threshold").asDouble(), 1e-9);
        assertFalse(response.get("body").get("clusters").isEmpty());
    }
}
