package phj.server;

import com.sun.net.httpserver.HttpServer;
import phj.Test;
import phj.TestRunner;
import phj.json.Json;

import java.net.InetSocketAddress;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.List;
import java.util.Map;
import java.util.concurrent.Executors;

/** 进程内启动内置 HTTP 服务做端到端测试。 */
public class HttpServerTest {

    private HttpServer server;
    private String base;

    private void start(int port) throws Exception {
        server = HttpServer.create(new InetSocketAddress(port), 0);
        server.createContext("/health", HttpServerMain::health);
        server.createContext("/query", HttpServerMain::query);
        server.setExecutor(Executors.newFixedThreadPool(2));
        server.start();
        base = "http://localhost:" + server.getAddress().getPort();
    }

    private void stop() {
        if (server != null) server.stop(0);
    }

    private HttpResponse<String> post(String body) throws Exception {
        HttpClient client = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(5)).build();
        HttpRequest req = HttpRequest.newBuilder(URI.create(base + "/query"))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(body, StandardCharsets.UTF_8))
                .build();
        return client.send(req, HttpResponse.BodyHandlers.ofString());
    }

    private static final String SAMPLE = """
            {
              "joinType": "LEFT",
              "keys": ["k"],
              "left":  {"name":"L","columns":["id","k"],"rows":[[1,"a"],[2,"b"],[3,null]]},
              "right": {"name":"R","columns":["cid","k"],"rows":[[10,"a"],[11,"b"],[12,"c"],[13,"d"]]},
              "options": {"memoryThresholdRows": 1, "partitions": 2, "diskQuotaBytes": 100000}
            }
            """;

    @Test
    public void healthOk() throws Exception {
        start(0);
        try {
            HttpClient client = HttpClient.newHttpClient();
            HttpResponse<String> r = client.send(
                    HttpRequest.newBuilder(URI.create(base + "/health")).GET().build(),
                    HttpResponse.BodyHandlers.ofString());
            TestRunner.assertEquals(200, r.statusCode());
            Map<?, ?> m = Json.asObj(Json.parse(r.body()), "health");
            TestRunner.assertEquals(Boolean.TRUE, m.get("ok"));
        } finally {
            stop();
        }
    }

    @Test
    public void querySuccess() throws Exception {
        start(0);
        try {
            HttpResponse<String> r = post(SAMPLE);
            TestRunner.assertEquals(200, r.statusCode());
            Map<?, ?> resp = Json.asObj(Json.parse(r.body()), "resp");
            TestRunner.assertEquals(Boolean.TRUE, resp.get("ok"));
            Map<?, ?> result = (Map<?, ?>) resp.get("result");
            // a、b 各匹配 1，null 补 NULL = 3
            TestRunner.assertEquals(3L, ((Number) result.get("rowCount")).longValue());
        } finally {
            stop();
        }
    }

    @Test
    public void queryBadRequestReturns400() throws Exception {
        start(0);
        try {
            HttpResponse<String> r = post("{bad json");
            TestRunner.assertEquals(400, r.statusCode());
            Map<?, ?> resp = Json.asObj(Json.parse(r.body()), "resp");
            TestRunner.assertEquals("INVALID_REQUEST", resp.get("errorType"));
        } finally {
            stop();
        }
    }

    @Test
    public void queryQuotaExceededReturns507() throws Exception {
        start(0);
        try {
            HttpResponse<String> r = post(SAMPLE.replace("100000", "0"));
            TestRunner.assertEquals(507, r.statusCode());
            Map<?, ?> resp = Json.asObj(Json.parse(r.body()), "resp");
            TestRunner.assertEquals("DISK_QUOTA_EXCEEDED", resp.get("errorType"));
        } finally {
            stop();
        }
    }

    @Test
    public void getOnQueryIs405() throws Exception {
        start(0);
        try {
            HttpClient client = HttpClient.newHttpClient();
            HttpResponse<String> r = client.send(
                    HttpRequest.newBuilder(URI.create(base + "/query")).GET().build(),
                    HttpResponse.BodyHandlers.ofString());
            TestRunner.assertEquals(405, r.statusCode());
        } finally {
            stop();
        }
    }
}
