package com.example.quantiles.service;

import com.example.quantiles.json.Json;
import com.example.quantiles.json.JsonParser;
import com.example.quantiles.json.JsonWriter;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

class QuantileHttpServerTest {

    private QuantileHttpServer server;
    private HttpClient client;
    private String base;

    @BeforeEach
    void setUp() throws Exception {
        server = new QuantileHttpServer(0); // 随机空闲端口
        server.start();
        base = "http://localhost:" + server.getPort();
        client = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(5)).build();
    }

    @AfterEach
    void tearDown() {
        server.stop(0);
    }

    @Test
    void healthEndpointReturnsOk() throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(base + "/health")).GET().build();
        HttpResponse<String> resp = client.send(req, HttpResponse.BodyHandlers.ofString());
        assertEquals(200, resp.statusCode());
        assertTrue(resp.body().contains("\"ok\""));
    }

    @Test
    void postQuantilesReturnsExactMedians() throws Exception {
        String body = """
                {
                  "windowSizeMillis": 10,
                  "windowSlideMillis": 5,
                  "quantiles": [0.5, 0.9],
                  "events": [
                    {"timestampMillis": 1, "value": -3},
                    {"timestampMillis": 2, "value": -1},
                    {"timestampMillis": 2, "value": 4},
                    {"timestampMillis": 12, "value": 100}
                  ]
                }
                """;
        HttpRequest req = HttpRequest.newBuilder(URI.create(base + "/quantiles"))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(body)).build();
        HttpResponse<String> resp = client.send(req, HttpResponse.BodyHandlers.ofString());
        assertEquals(200, resp.statusCode(), resp.body());
        Map<String, Object> out = Json.asObject(JsonParser.parse(resp.body()));
        assertEquals(4L, Json.asLong(out.get("eventCount")));
        List<Map<String, Object>> results = Json.asArray(out.get("results")).stream()
                .map(Json::asObject).toList();
        // 首窗 [-5,10)（首个 pane 能落入的最早窗口）：事件 -3,-1,4 均在内；
        // 后续 [0,10)、[5,15) 等窗口也会输出
        List<Object> firstValues = Json.asArray(results.get(0).get("values"));
        assertEquals("-1", JsonWriter.write(firstValues.get(0)));
    }

    @Test
    void invalidJsonReturns400() throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(base + "/quantiles"))
                .POST(HttpRequest.BodyPublishers.ofString("{not json")).build();
        HttpResponse<String> resp = client.send(req, HttpResponse.BodyHandlers.ofString());
        assertEquals(400, resp.statusCode());
        assertTrue(resp.body().contains("error"));
    }

    @Test
    void missingRequiredFieldReturns400() throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(base + "/quantiles"))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString("{\"events\":[]}")).build();
        HttpResponse<String> resp = client.send(req, HttpResponse.BodyHandlers.ofString());
        assertEquals(400, resp.statusCode());
    }

    @Test
    void wrongMethodReturns405() throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(base + "/quantiles")).GET().build();
        HttpResponse<String> resp = client.send(req, HttpResponse.BodyHandlers.ofString());
        assertEquals(405, resp.statusCode());
    }
}
