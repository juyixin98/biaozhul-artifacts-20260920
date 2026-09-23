package com.example.tvl.tests;

import com.example.tvl.http.HttpServerMain;
import com.example.tvl.json.Json;
import com.sun.net.httpserver.HttpServer;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static com.example.tvl.tests.MiniTest.assertEquals;
import static com.example.tvl.tests.MiniTest.assertTrue;


/** 启动真实 HTTP 服务（临时端口）做端到端验证。 */
public final class HttpEndToEndTest {

    private static Map<String, Object> request() {
        Map<String, Object> req = new LinkedHashMap<>();
        req.put("sql", "SELECT id, name FROM t WHERE score >= ? AND name IS NOT NULL");
        req.put("schema", Map.of("columns", List.of(
                Map.of("name", "id", "type", "INTEGER"),
                Map.of("name", "score", "type", "FLOAT"),
                Map.of("name", "name", "type", "TEXT"))));
        req.put("params", List.of(Map.of("type", "FLOAT", "value", 8.0)));
        req.put("batches", List.of(
                Map.of("rows", java.util.Arrays.asList(
                        java.util.Arrays.asList(1, 9.5, "alice"),
                        java.util.Arrays.asList(2, null, "bob"),
                        java.util.Arrays.asList(3, 7.0, null))),
                Map.of("rows", List.of())));
        return req;
    }

    public static MiniTest.Suite suite() {
        return MiniTest.suite("HTTP 端到端（JDK HttpServer + HttpClient）")

                .test("POST /query：过滤、UNKNOWN 排除、空批次、跨批次", () -> {
                    HttpServer server = new HttpServerMain().start(0);
                    try {
                        int port = server.getAddress().getPort();
                        HttpClient client = HttpClient.newHttpClient();
                        HttpResponse<String> resp = post(client, port, Json.write(request()));

                        assertEquals(200, resp.statusCode(), "状态码");
                        @SuppressWarnings("unchecked")
                        Map<String, Object> body = (Map<String, Object>) Json.parse(resp.body());
                        assertEquals(true, body.get("ok"), "ok");
                        assertEquals(2, body.get("batchCount"), "两个批次");
                        assertEquals(1, body.get("totalSelected"), "仅 alice 被选中");

                        @SuppressWarnings("unchecked")
                        List<Map<String, Object>> results = (List<Map<String, Object>>) body.get("results");
                        assertEquals(3, results.get(0).get("rowCount"), "批次1 三行");
                        assertEquals(1, results.get(0).get("unknownRows"), "bob 的 score NULL → UNKNOWN");
                        assertEquals(1, results.get(0).get("falseRows"), "7.0>=8 且 name NULL → FALSE");
                        assertEquals(0, results.get(1).get("rowCount"), "批次2 空");

                        @SuppressWarnings("unchecked")
                        List<List<Object>> rows = (List<List<Object>>) results.get(0).get("rows");
                        assertEquals(List.of(1L, "alice"), rows.get(0), "结果行内容");

                        @SuppressWarnings("unchecked")
                        List<String> tvl = (List<String>) results.get(0).get("tvl");
                        assertEquals(List.of("TRUE", "UNKNOWN", "FALSE"), tvl, "逐行三值对照");
                    } finally {
                        server.stop(0);
                    }
                })

                .test("参数类型错误返回 400 SEMANTIC_ERROR", () -> {
                    HttpServer server = new HttpServerMain().start(0);
                    try {
                        int port = server.getAddress().getPort();
                        Map<String, Object> bad = request();
                        bad.put("params", List.of(Map.of("type", "FLOAT", "value", "8"))); // 字符串
                        HttpResponse<String> resp = post(HttpClient.newHttpClient(), port, Json.write(bad));
                        assertEquals(400, resp.statusCode(), "状态码 400");
                        @SuppressWarnings("unchecked")
                        Map<String, Object> body = (Map<String, Object>) Json.parse(resp.body());
                        assertEquals("SEMANTIC_ERROR", body.get("errorKind"), "错误类别");
                        assertTrue(String.valueOf(body.get("error")).contains("拒绝隐式字符串转数值"),
                                "错误信息说明拒绝混转: " + body.get("error"));
                    } finally {
                        server.stop(0);
                    }
                })

                .test("文本数值混转比较返回 400", () -> {
                    HttpServer server = new HttpServerMain().start(0);
                    try {
                        int port = server.getAddress().getPort();
                        Map<String, Object> bad = request();
                        bad.put("sql", "SELECT id FROM t WHERE name = 8");
                        bad.remove("params");
                        HttpResponse<String> resp = post(HttpClient.newHttpClient(), port, Json.write(bad));
                        assertEquals(400, resp.statusCode(), "状态码 400");
                        @SuppressWarnings("unchecked")
                        Map<String, Object> body = (Map<String, Object>) Json.parse(resp.body());
                        assertEquals("SEMANTIC_ERROR", body.get("errorKind"), "类型不匹配类别");
                    } finally {
                        server.stop(0);
                    }
                })

                .test("SQL 语法错误返回 400 SQL_PARSE_ERROR", () -> {
                    HttpServer server = new HttpServerMain().start(0);
                    try {
                        int port = server.getAddress().getPort();
                        Map<String, Object> bad = request();
                        bad.put("sql", "SELECT id FROM");
                        HttpResponse<String> resp = post(HttpClient.newHttpClient(), port, Json.write(bad));
                        assertEquals(400, resp.statusCode(), "400");
                        @SuppressWarnings("unchecked")
                        Map<String, Object> body = (Map<String, Object>) Json.parse(resp.body());
                        assertEquals("SQL_PARSE_ERROR", body.get("errorKind"), "解析错误类别");
                    } finally {
                        server.stop(0);
                    }
                })

                .test("非法 JSON 返回 400 JSON_ERROR；GET /health 返回 ok", () -> {
                    HttpServer server = new HttpServerMain().start(0);
                    try {
                        int port = server.getAddress().getPort();
                        HttpClient client = HttpClient.newHttpClient();
                        HttpRequest req = HttpRequest.newBuilder(URI.create("http://localhost:" + port + "/query"))
                                .header("Content-Type", "application/json")
                                .POST(HttpRequest.BodyPublishers.ofString("{not json"))
                                .build();
                        HttpResponse<String> resp = client.send(req, HttpResponse.BodyHandlers.ofString());
                        assertEquals(400, resp.statusCode(), "非法 JSON 400");

                        HttpResponse<String> health = client.send(
                                HttpRequest.newBuilder(URI.create("http://localhost:" + port + "/health")).GET().build(),
                                HttpResponse.BodyHandlers.ofString());
                        assertEquals(200, health.statusCode(), "health 200");
                        assertTrue(health.body().contains("\"ok\""), "health 含 ok: " + health.body());
                    } finally {
                        server.stop(0);
                    }
                });
    }

    private static HttpResponse<String> post(HttpClient client, int port, String body) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create("http://localhost:" + port + "/query"))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(body))
                .build();
        return client.send(req, HttpResponse.BodyHandlers.ofString());
    }

    private HttpEndToEndTest() {}
}
